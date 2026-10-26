"""队列服务层：任务模型、子进程调度、进度事件、进程组管理与持久化。

零 Textual 依赖、禁止 print。UI 通过 ``events`` 队列消费快照事件，
自身不直接等待子进程；异常通过事件 / 任务状态表达。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_queue_path
from .ytdlp import (
    YtdlpError,
    build_args,
    classify_error,
    parse_output_line,
    progress_percent,
)

QUEUED = "queued"
RUNNING = "running"
POSTPROCESSING = "postprocessing"
PAUSED = "paused"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE_STATUSES = {QUEUED, RUNNING, POSTPROCESSING, PAUSED, INTERRUPTED}
LIVE_STATUSES = {RUNNING, POSTPROCESSING}
FINISHED_STATUSES = {COMPLETED, FAILED, CANCELLED}

STATUS_LABELS = {
    QUEUED: "等待中",
    RUNNING: "下载中",
    POSTPROCESSING: "后处理",
    PAUSED: "已暂停",
    INTERRUPTED: "已中断",
    COMPLETED: "已完成",
    FAILED: "失败",
    CANCELLED: "已停止",
}

MAX_LOG_LINES = 300
SAVE_INTERVAL = 5.0
KILL_GRACE = 3.0

IS_WINDOWS = os.name == "nt"

# POSIX 才有 killpg/getpgid：用它把 yt-dlp 与 ffmpeg 作为一个进程组一起管理
POSIX_GROUP_SIGNALS = os.name == "posix" and hasattr(os, "killpg") and hasattr(os, "getpgid")

# start_new_session 是 POSIX 概念，Windows 不认（会走 CreateProcess 的 flags 语义）
SESSION_KWARGS: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}


@dataclass
class Task:
    """单个下载任务（可序列化为 queue.json 记录）。"""

    id: str
    url: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    preset: str = "best"
    format_id: str = ""
    container: str = "auto"
    playlist_items: str = ""
    is_playlist: bool = False
    status: str = QUEUED
    percent: float = 0.0
    speed: float = 0.0
    eta: int = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    filepath: str = ""
    error: str = ""
    error_hint: str = ""
    cmd: list[str] = field(default_factory=list)
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    log_tail: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or self.url

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "uploader": self.uploader,
            "duration": self.duration,
            "preset": self.preset,
            "format_id": self.format_id,
            "container": self.container,
            "playlist_items": self.playlist_items,
            "is_playlist": self.is_playlist,
            "status": self.status,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "filepath": self.filepath,
            "error": self.error,
            "error_hint": self.error_hint,
            "cmd": list(self.cmd),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_tail": list(self.log_tail),
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        def num(key: str, default: float = 0.0) -> float:
            try:
                return float(raw.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        log_tail = raw.get("log_tail")
        overrides = raw.get("overrides")
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            url=str(raw.get("url") or ""),
            title=str(raw.get("title") or ""),
            uploader=str(raw.get("uploader") or ""),
            duration=num("duration"),
            preset=str(raw.get("preset") or "best"),
            format_id=str(raw.get("format_id") or ""),
            container=str(raw.get("container") or "auto"),
            playlist_items=str(raw.get("playlist_items") or ""),
            is_playlist=bool(raw.get("is_playlist", False)),
            status=str(raw.get("status") or QUEUED),
            percent=num("percent"),
            speed=num("speed"),
            eta=int(num("eta")),
            downloaded_bytes=int(num("downloaded_bytes")),
            total_bytes=int(num("total_bytes")),
            filepath=str(raw.get("filepath") or ""),
            error=str(raw.get("error") or ""),
            error_hint=str(raw.get("error_hint") or ""),
            cmd=[str(c) for c in raw.get("cmd") or []],
            created_at=num("created_at", time.time()),
            started_at=num("started_at"),
            finished_at=num("finished_at"),
            log_tail=[str(x) for x in log_tail][-MAX_LOG_LINES:] if isinstance(log_tail, list) else [],
            overrides=dict(overrides) if isinstance(overrides, dict) else {},
        )

    def append_log(self, line: str) -> None:
        if not line:
            return
        self.log_tail.append(line)
        if len(self.log_tail) > MAX_LOG_LINES:
            del self.log_tail[: len(self.log_tail) - MAX_LOG_LINES]


@dataclass
class QueueEvent:
    """队列事件：kind = task（状态迁移）/ progress（进度）/ tasks（集合变化）。"""

    kind: str
    task_id: str
    payload: dict[str, Any]


class QueueManager:
    """asyncio 子进程调度器：并发上限、取消 / 暂停、事件推送与持久化。"""

    def __init__(
        self,
        settings_provider: Callable[[], dict[str, Any]],
        *,
        path: Path | None = None,
    ) -> None:
        self._settings = settings_provider
        self.path = path or get_queue_path()
        self.tasks: dict[str, Task] = {}
        self.events: asyncio.Queue[QueueEvent] = asyncio.Queue()
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._cancel_requested: set[str] = set()
        self._dirty = False
        self._closing = False
        self._started = False
        self._save_job: asyncio.Task[None] | None = None
        self._killer_jobs: set[asyncio.Task[None]] = set()
        self.load()

    # ------------------------------------------------------------ 持久化

    def load(self) -> None:
        """读取 queue.json；上次未完成的任务标为 interrupted（等待用户确认续传）。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        items = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            task = Task.from_dict(item)
            if not task.url:
                continue
            if task.status in (RUNNING, POSTPROCESSING, PAUSED):
                task.status = INTERRUPTED
                task.speed = 0.0
                task.eta = 0
            self.tasks[task.id] = task

    @property
    def interrupted(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == INTERRUPTED]

    def _payload(self) -> dict[str, Any]:
        return {"version": 1, "tasks": [t.to_dict() for t in self.tasks.values()]}

    def _write(self) -> None:
        self._trim_history()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._payload(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # 持久化失败不影响下载，下一次状态迁移会再尝试
            return
        self._dirty = False

    def _mark_dirty(self) -> None:
        self._dirty = True

    def save_now(self) -> None:
        self._write()

    def _trim_history(self) -> None:
        try:
            limit = int(self._settings().get("history_limit", 200))
        except (TypeError, ValueError):
            limit = 200
        finished = [t for t in self.tasks.values() if t.status in FINISHED_STATUSES]
        if len(finished) <= limit:
            return
        finished.sort(key=lambda t: t.finished_at or t.created_at)
        for task in finished[: len(finished) - limit]:
            self.tasks.pop(task.id, None)

    async def _save_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(SAVE_INTERVAL)
            except asyncio.CancelledError:
                raise
            if self._dirty:
                self._write()

    # ------------------------------------------------------------ 事件

    def _emit(self, kind: str, task: Task | None = None, task_id: str = "") -> None:
        tid = task.id if task is not None else task_id
        # 进度事件每秒可达数十条，消费方只按 task_id 增量更新；不序列化整条任务
        # （尤其是 300 行 log_tail），避免高频分配。
        payload = task.to_dict() if (task is not None and kind != "progress") else {}
        with contextlib.suppress(asyncio.QueueFull):
            self.events.put_nowait(QueueEvent(kind, tid, payload))

    def _emit_state(self, task: Task) -> None:
        self._emit("task", task)
        self._mark_dirty()
        self._write()

    # ------------------------------------------------------------ 调度

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._save_job = asyncio.create_task(self._save_loop())
        self.pump()

    def _max_concurrent(self) -> int:
        try:
            value = int(self._settings().get("max_concurrent_tasks", 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(8, value))

    def pump(self) -> None:
        """按并发上限启动排队任务（幂等，可在任意状态变化后调用）。"""
        if self._closing:
            return
        live = sum(1 for t in self.tasks.values() if t.status in LIVE_STATUSES)
        limit = self._max_concurrent()
        if live >= limit:
            return
        pending = sorted(
            (t for t in self.tasks.values() if t.status == QUEUED),
            key=lambda t: t.created_at,
        )
        for task in pending:
            if live >= limit:
                break
            task.status = RUNNING
            live += 1
            self._runners[task.id] = asyncio.create_task(self._run(task))

    def apply_settings(self) -> None:
        """设置变更后热更新并发（立即尝试补齐空闲槽位）。"""
        self.pump()

    # ------------------------------------------------------------ 增删改

    def add(
        self,
        *,
        url: str,
        title: str = "",
        uploader: str = "",
        duration: float = 0.0,
        preset: str = "best",
        format_id: str = "",
        container: str = "auto",
        playlist_items: str = "",
        is_playlist: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> Task:
        task_id = uuid.uuid4().hex[:8]
        while task_id in self.tasks:
            task_id = uuid.uuid4().hex[:8]
        task = Task(
            id=task_id,
            url=url,
            title=title,
            uploader=uploader,
            duration=duration,
            preset=preset,
            format_id=format_id,
            container=container,
            playlist_items=playlist_items,
            is_playlist=is_playlist,
            status=QUEUED,
            created_at=time.time(),
            overrides=dict(overrides or {}),
        )
        self.tasks[task.id] = task
        self._emit_state(task)
        self._emit("tasks", task_id=task.id)
        self.pump()
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def counts(self) -> dict[str, int]:
        result = {"running": 0, "queued": 0, "paused": 0, "interrupted": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for task in self.tasks.values():
            key = {
                RUNNING: "running",
                POSTPROCESSING: "running",
                QUEUED: "queued",
                PAUSED: "paused",
                INTERRUPTED: "interrupted",
                COMPLETED: "completed",
                FAILED: "failed",
                CANCELLED: "cancelled",
            }.get(task.status)
            if key:
                result[key] += 1
        return result

    def total_speed(self) -> float:
        return sum(t.speed for t in self.tasks.values() if t.status in LIVE_STATUSES)

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ACTIVE_STATUSES)

    def resume_interrupted(self) -> int:
        """把上次中断的任务重新排队（保留 .part 断点续传）。"""
        tasks = self.interrupted
        for task in tasks:
            task.status = QUEUED
            task.error = ""
            task.error_hint = ""
            task.percent = 0.0
            task.speed = 0.0
            task.eta = 0
            self._emit("task", task)
        if tasks:
            self._emit("tasks", task_id="")
            self._mark_dirty()
            self._write()
            self.pump()
        return len(tasks)

    async def retry(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.status in LIVE_STATUSES or task.status == PAUSED:
            return False
        task.status = QUEUED
        task.percent = 0.0
        task.speed = 0.0
        task.eta = 0
        task.downloaded_bytes = 0
        task.error = ""
        task.error_hint = ""
        task.filepath = ""
        task.started_at = 0.0
        task.finished_at = 0.0
        self._emit_state(task)
        self._emit("tasks", task_id=task.id)
        self.pump()
        return True

    async def remove(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        if task.status in LIVE_STATUSES or task.status == PAUSED:
            await self.cancel(task_id)
        self.tasks.pop(task_id, None)
        self._emit("tasks", task_id=task_id)
        self._mark_dirty()
        self._write()
        self.pump()
        return True

    # ------------------------------------------------------------ 进程控制

    @staticmethod
    def can_pause() -> bool:
        return POSIX_GROUP_SIGNALS

    @staticmethod
    def _signal_proc(proc: asyncio.subprocess.Process, sig: int) -> bool:
        """给单个进程发信号（POSIX 下连带整个进程组）。"""
        if proc.returncode is not None:
            return False
        if POSIX_GROUP_SIGNALS:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.send_signal(sig)
            return True
        except (ProcessLookupError, PermissionError, OSError, ValueError):
            return False

    @staticmethod
    def _terminate(proc: asyncio.subprocess.Process) -> bool:
        """请求终止：POSIX 杀进程组；Windows 走原生 terminate。"""
        if proc.returncode is not None:
            return False
        if POSIX_GROUP_SIGNALS:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.terminate()
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """强制结束进程：POSIX 杀进程组；Windows 用 taskkill /T 连带 ffmpeg。"""
        if proc.returncode is not None:
            return
        if POSIX_GROUP_SIGNALS:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        if IS_WINDOWS:
            # Windows 无进程组信号，用 taskkill /T /F 收集子进程（合并 / 后处理时的 ffmpeg）
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                proc.kill()

    async def cancel(self, task_id: str) -> None:
        """停止任务：排队中直接置位；运行中终止整个进程树（连带 ffmpeg）。"""
        task = self.tasks.get(task_id)
        if task is None:
            return
        if task.status in (QUEUED, INTERRUPTED):
            task.status = CANCELLED
            task.finished_at = time.time()
            self._emit_state(task)
            self._emit("tasks", task_id=task.id)
            self.pump()
            return
        if task.status in (COMPLETED, FAILED, CANCELLED):
            return

        self._cancel_requested.add(task_id)
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return
        if task.status == PAUSED and POSIX_GROUP_SIGNALS:
            # 暂停态先恢复，否则 SIGTERM 会滞留在停止的进程里
            self._signal_proc(proc, signal.SIGCONT)
        self._terminate(proc)
        self._emit("task", task)
        job = asyncio.create_task(self._force_kill(task_id, proc))
        self._killer_jobs.add(job)
        job.add_done_callback(self._killer_jobs.discard)

    async def _force_kill(self, task_id: str, proc: asyncio.subprocess.Process) -> None:
        await asyncio.sleep(KILL_GRACE)
        current = self._procs.get(task_id) or proc
        if current.returncode is None:
            await self._kill_tree(current)

    def pause(self, task_id: str) -> bool:
        if not self.can_pause():
            return False
        task = self.tasks.get(task_id)
        proc = self._procs.get(task_id)
        if task is None or proc is None or task.status != RUNNING:
            return False
        if not self._signal_proc(proc, signal.SIGSTOP):
            return False
        task.status = PAUSED
        task.speed = 0.0
        task.eta = 0
        self._emit_state(task)
        return True

    def resume(self, task_id: str) -> bool:
        if not self.can_pause():
            return False
        task = self.tasks.get(task_id)
        proc = self._procs.get(task_id)
        if task is None or proc is None or task.status != PAUSED:
            return False
        if not self._signal_proc(proc, signal.SIGCONT):
            return False
        task.status = RUNNING
        self._emit_state(task)
        return True

    async def shutdown(self) -> None:
        """退出前清理：终止全部子进程并落盘。"""
        if self._closing:
            return
        self._closing = True
        if self._save_job is not None:
            self._save_job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._save_job
            self._save_job = None
        live = [p for p in self._procs.values() if p.returncode is None]
        for proc in live:
            if POSIX_GROUP_SIGNALS:
                self._signal_proc(proc, signal.SIGCONT)
            self._terminate(proc)
        runners = [r for r in self._runners.values() if not r.done()]
        if runners:
            _, pending = await asyncio.wait(runners, timeout=KILL_GRACE)
            for proc in live:
                await self._kill_tree(proc)
            if pending:
                await asyncio.wait(pending, timeout=2.0)
        for job in list(self._killer_jobs):
            job.cancel()
        self._write()

    # ------------------------------------------------------------ 运行单任务

    async def _run(self, task: Task) -> None:
        # 程序正在退出：不再启动新的子进程
        if self._closing:
            task.status = INTERRUPTED
            self._runners.pop(task.id, None)
            return
        # 排队期间被取消（此时还没拿到进程）：直接落 cancelled，不启动子进程
        if task.id in self._cancel_requested:
            self._cancel_requested.discard(task.id)
            task.status = CANCELLED
            task.finished_at = time.time()
            self._runners.pop(task.id, None)
            self._emit_state(task)
            self._emit("tasks", task_id=task.id)
            self.pump()
            return
        settings = self._settings()
        task.started_at = time.time()
        task.finished_at = 0.0
        task.percent = 0.0
        task.speed = 0.0
        task.eta = 0
        task.downloaded_bytes = 0
        task.total_bytes = 0
        task.error = ""
        task.error_hint = ""
        try:
            args = build_args(task.to_dict(), settings)
        except YtdlpError as exc:
            self._finish_failed(task, str(exc), str(exc))
            return
        task.cmd = args
        self._emit_state(task)

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                **SESSION_KWARGS,
            )
        except OSError as exc:
            self._finish_failed(task, f"无法启动下载进程：{exc}", "")
            return

        self._procs[task.id] = proc
        if self._closing:
            # 启动与退出竞态：立刻终止刚拉起的进程
            self._terminate(proc)
        returncode = -1
        try:
            stream = proc.stdout
            if stream is not None:
                while True:
                    raw = await stream.readline()
                    if not raw:
                        break
                    self._consume_line(task, raw.decode("utf-8", errors="replace"))
            returncode = await proc.wait()
        except asyncio.CancelledError:
            returncode = -1
            raise
        finally:
            self._procs.pop(task.id, None)
            self._runners.pop(task.id, None)

        cancelled = task.id in self._cancel_requested
        self._cancel_requested.discard(task.id)
        task.speed = 0.0
        task.eta = 0
        task.finished_at = time.time()
        if self._closing:
            # 退出程序导致的中断：保留为可续传状态，下次启动提示继续
            task.status = INTERRUPTED
        elif cancelled:
            task.status = CANCELLED
        elif returncode == 0:
            task.status = COMPLETED
            task.percent = 100.0
            if not task.filepath:
                task.filepath = self._guess_filepath(task, settings)
        else:
            task.status = FAILED
            if not task.error:
                task.error = f"yt-dlp 退出码 {returncode}"
            task.error_hint = classify_error("\n".join(task.log_tail[-20:])) or "下载失败，请查看详情日志"
        self._emit_state(task)
        self._emit("tasks", task_id=task.id)
        self.pump()

    def _finish_failed(self, task: Task, error: str, hint: str) -> None:
        task.status = FAILED
        task.error = error
        task.error_hint = hint or "下载失败，请查看详情日志"
        task.speed = 0.0
        task.eta = 0
        task.finished_at = time.time()
        task.append_log(f"ERROR: {error}")
        self._emit_state(task)
        self._emit("tasks", task_id=task.id)
        self._runners.pop(task.id, None)
        self.pump()

    def _consume_line(self, task: Task, line: str) -> None:
        kind, payload = parse_output_line(line)
        if kind == "progress":
            task.percent = progress_percent(payload)
            if payload.get("status") == "finished":
                task.percent = 100.0
            downloaded = payload.get("downloaded_bytes")
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
            if isinstance(downloaded, (int, float)):
                task.downloaded_bytes = int(downloaded)
            if isinstance(total, (int, float)):
                task.total_bytes = int(total)
            speed = payload.get("speed")
            task.speed = float(speed) if isinstance(speed, (int, float)) else 0.0
            eta = payload.get("eta")
            task.eta = int(eta) if isinstance(eta, (int, float)) else 0
            self._mark_dirty()
            self._emit("progress", task)
            return

        if kind == "postprocess":
            name = str(payload.get("postprocessor") or "")
            status = str(payload.get("status") or "")
            if status == "started":
                if task.status == RUNNING:
                    task.status = POSTPROCESSING
                    self._emit_state(task)
                task.append_log(f"[后处理] {name} 开始")
            elif status == "finished":
                task.append_log(f"[后处理] {name} 完成")
            return

        if kind == "final":
            if payload:
                task.filepath = str(payload)
                self._mark_dirty()
            return

        text = str(payload)
        if not text:
            return
        task.append_log(text)
        if "ERROR:" in text:
            task.error = text

    def _guess_filepath(self, task: Task, settings: dict[str, Any]) -> str:
        """没有收到 FINAL 行时，按修改时间在下载目录里兜底定位。"""
        directory = Path(str(settings.get("download_dir", "~/Downloads/yt-dlp"))).expanduser()
        since = task.started_at - 2.0
        newest: tuple[float, Path] | None = None
        try:
            for path in directory.iterdir():
                if not path.is_file() or path.suffix == ".part":
                    continue
                mtime = path.stat().st_mtime
                if mtime >= since and (newest is None or mtime > newest[0]):
                    newest = (mtime, path)
        except OSError:
            return ""
        return str(newest[1]) if newest else ""
