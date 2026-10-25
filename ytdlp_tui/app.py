"""Textual 应用：全局配置、队列单例、状态栏、启动恢复与退出保护。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.spinner import SPINNERS
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from . import __version__
from .config import load_config
from .queue import QueueManager
from .widgets import ConfirmModal, ProgressCover
from .ytdlp import human_speed

_DOTS = SPINNERS["dots"]


class StatusBar(Static):
    """底部状态栏：展示最近一次操作结果 / 进行中提示；spinner=True 时显示点阵转圈。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = ""
        self._busy = False
        self._frame = 0
        self._timer = None

    def render(self) -> Text:
        if self._busy:
            glyph = _DOTS["frames"][self._frame % len(_DOTS["frames"])]
            return Text.assemble((glyph, "bold"), " ", self._text)
        return Text(self._text)

    def set_status(self, text: str) -> None:
        self._busy = False
        self._text = text
        self._stop_timer()
        self.refresh()

    def set_spinner(self, text: str) -> None:
        self._busy = True
        self._text = text
        self._ensure_timer()
        self.refresh()

    def _ensure_timer(self) -> None:
        if self._timer is None and self.is_mounted:
            self._timer = self.set_interval(_DOTS["interval"] / 1000, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def on_mount(self) -> None:
        self._ensure_timer()


class YtdlpTuiApp(App):
    """yt-dlp 图形化终端界面。"""

    CSS_PATH = Path(__file__).parent / "app.tcss"
    TITLE = "yt-dlp TUI"
    SUB_TITLE = f"v{__version__}"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: dict = {}
        self._queue: QueueManager | None = None
        self._quitting = False
        self._quit_pending = False
        self._booted = False

    # ------------------------------------------------------------ 全局资源

    def get_queue(self) -> QueueManager:
        """队列管理器单例（延迟创建，首次访问时加载 queue.json）。"""
        if self._queue is None:
            self._queue = QueueManager(lambda: self.config)
        return self._queue

    def get_settings(self) -> dict:
        return self.config

    def reload_config(self) -> list[str]:
        """重新读取配置并返回告警（坏值已回落，不阻断启动）。"""
        config, warnings = load_config()
        self.config = config
        return warnings

    # ------------------------------------------------------------ 状态栏 / 提示

    def compose(self) -> ComposeResult:
        yield StatusBar("", id="status")

    def get_loading_widget(self) -> Widget:
        """全局默认：任意控件 .loading=True 时用 indeterminate 进度条覆盖其区域。"""
        return ProgressCover()

    def status(self, message: str, kind: str = "") -> None:
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:
            return
        bar.update_classes({"ok": kind == "ok", "warn": kind == "warn", "err": kind == "err"})
        bar.set_status(message)

    def spinner(self, on: bool, message: str = "加载中…") -> None:
        """状态栏点阵 spinner（无遮罩、不阻断输入）：适合轻量、即时的后台动作。"""
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:
            return
        bar.update_classes({})
        if on:
            bar.set_spinner(message)
        else:
            bar.set_status("")

    def notify_ok(self, message: str) -> None:
        self.status(message, "ok")
        self.notify(message, severity="success", timeout=4)

    def notify_err(self, message: str) -> None:
        self.status(message, "err")
        self.notify(message, severity="error", timeout=8)

    def notify_warn(self, message: str) -> None:
        self.status(message, "warn")
        self.notify(message, severity="warning", timeout=5)

    # ------------------------------------------------------------ 退出

    def action_quit(self) -> None:
        """全局退出（Ctrl+Q / 首页退出）：有未完成任务时先确认。"""
        if self._quitting or self._quit_pending:
            return
        queue = self.get_queue()
        active = queue.active_count()
        if active > 0:
            self._quit_pending = True
            self.push_screen(
                ConfirmModal(
                    f"仍有 {active} 个任务进行中，退出将中断下载。确定退出吗？",
                    title="退出确认",
                    yes="退出",
                    no="继续下载",
                    default_yes=False,
                ),
                self._on_quit_confirmed,
            )
            return
        self._shutdown_and_exit()

    def _on_quit_confirmed(self, confirmed: bool) -> None:
        self._quit_pending = False
        if confirmed:
            self._shutdown_and_exit()

    @work
    async def _shutdown_and_exit(self) -> None:
        self._quitting = True
        try:
            await self.get_queue().shutdown()
        finally:
            self.exit()

    # ------------------------------------------------------------ 启动

    def on_mount(self) -> None:
        self._boot()

    @work
    async def _boot(self) -> None:
        if self._booted:
            return
        self._booted = True
        try:
            warnings = self.reload_config()
        except Exception as exc:  # 配置层不应抛错，兜底避免白屏
            self.notify_err(f"配置加载失败：{exc}")
            warnings = []
        for warning in warnings:
            self.notify_warn(warning)

        from .screens.home import HomeScreen

        self.push_screen(HomeScreen())

        queue = self.get_queue()
        interrupted = queue.interrupted
        if interrupted:
            self.status(f"{len(interrupted)} 个任务上次被中断，等待确认续传", "warn")
            resume = await self.push_screen_wait(
                ConfirmModal(
                    f"检测到 {len(interrupted)} 个任务上次被中断，是否继续下载（保留 .part 断点续传）？",
                    title="恢复任务",
                    yes="继续下载",
                    no="稍后处理",
                    default_yes=True,
                )
            )
            if resume:
                count = queue.resume_interrupted()
                self.notify_ok(f"已重新排队 {count} 个任务")
        queue.start()
        self._consume_queue_events()

    @work(exclusive=True)
    async def _consume_queue_events(self) -> None:
        """统一消费队列事件：每 200ms 批量 drain 一次，再分发给栈内各屏。

        单消费者避免「队列页 / 历史页同时挂载时互相抢事件」；每屏按 task id
        增量更新单元格，不做整表重建。
        """
        queue = self.get_queue()
        while True:
            first = await queue.events.get()
            batch = [first]
            await asyncio.sleep(0.2)
            while not queue.events.empty():
                batch.append(queue.events.get_nowait())
            for screen in list(self.screen_stack):
                handler = getattr(screen, "apply_queue_events", None)
                if not callable(handler):
                    continue
                try:
                    handler(batch)
                except Exception:
                    # 单个屏幕刷新失败不应中断全局事件消费，下一批事件或重进页面会自愈
                    continue

    # ------------------------------------------------------------ 汇总文案

    def queue_summary(self) -> str:
        queue = self.get_queue()
        counts = queue.counts()
        parts = []
        if counts["running"]:
            parts.append(f"↓ {human_speed(queue.total_speed())}")
            parts.append(f"{counts['running']} 进行中")
        if counts["queued"]:
            parts.append(f"{counts['queued']} 等待")
        if counts["paused"]:
            parts.append(f"{counts['paused']} 暂停")
        if counts["failed"]:
            parts.append(f"{counts['failed']} 失败")
        if not parts:
            parts.append(f"{counts['completed']} 已完成")
        return " · ".join(parts)


__all__ = ["YtdlpTuiApp", "StatusBar"]
