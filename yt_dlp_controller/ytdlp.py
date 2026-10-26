"""yt-dlp 服务层：可执行文件探测、参数构建、链接解析、输出行解析与错误归类。

零 Textual 依赖、禁止 print；失败统一抛 YtdlpError，由屏幕层提示。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AUDIO_PRESETS, PRESET_KEYS, PRESETS

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

PROBE_TIMEOUT = 60.0
VERSION_TIMEOUT = 15.0


class YtdlpError(RuntimeError):
    """yt-dlp 调用失败（含中文可读信息）。"""


@dataclass
class ProbeResult:
    """链接解析结果。"""

    kind: str = "video"  # video | playlist
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    webpage_url: str = ""
    video_id: str = ""
    playlist_count: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)
    formats: list[dict[str, Any]] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        return self.playlist_count or len(self.entries)

    @property
    def is_playlist(self) -> bool:
        return self.kind == "playlist"


# ---------------------------------------------------------------- 可执行文件


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _resolve_binary(configured: str, name: str) -> str:
    """解析可执行文件路径；配置为空时走 PATH 探测。"""
    configured = (configured or "").strip()
    candidate = configured or name
    if os.path.sep in candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        # 配置里保存的路径可能因环境变化失效（换机器 / 升级后路径变动）：回退 PATH
        found = shutil.which(name)
        if found:
            return found
        raise YtdlpError(f"找不到可执行文件：{candidate}，请检查设置中的路径")
    found = shutil.which(candidate)
    if found:
        return found
    if configured:
        raise YtdlpError(f"找不到可执行文件：{configured}，请检查设置中的路径")
    raise YtdlpError(f"未找到 {name}，请先安装并确保它在 PATH 中")


def ytdlp_binary(settings: dict[str, Any]) -> str:
    return _resolve_binary(str(settings.get("ytdlp_path", "")), "yt-dlp")


def ffmpeg_binary(settings: dict[str, Any]) -> str:
    """ffmpeg 缺失不阻止下载（仅部分后处理需要），找不到返回空串。"""
    try:
        return _resolve_binary(str(settings.get("ffmpeg_path", "")), "ffmpeg")
    except YtdlpError:
        return ""


def version_of(binary: str) -> str:
    """同步读取版本号（应在 to_thread 中调用）。"""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise YtdlpError(f"无法执行 {binary}：{exc}") from exc
    text = strip_ansi((proc.stdout or proc.stderr).strip())
    first = text.splitlines()[0].strip() if text else ""
    return first


def ffmpeg_version(binary: str) -> str:
    if not binary:
        return ""
    try:
        proc = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (proc.stdout or "").splitlines()
    if not first:
        return ""
    match = re.search(r"ffmpeg version (\S+)", first[0])
    return match.group(1) if match else ""


def check_update(settings: dict[str, Any]) -> tuple[bool, str]:
    """执行 yt-dlp -U，返回 (是否成功, 输出文本)。"""
    binary = ytdlp_binary(settings)
    args = [binary, "-U", *network_args(settings)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise YtdlpError(f"检查更新失败：{exc}") from exc
    output = strip_ansi(((proc.stdout or "") + (proc.stderr or "")).strip())
    return proc.returncode == 0, output or "（无输出）"


def open_directory(path: str) -> None:
    """用系统文件管理器打开目录；失败抛 YtdlpError。"""
    target = Path(path).expanduser()
    if not target.exists():
        raise YtdlpError(f"路径不存在：{target}")
    if target.is_file():
        target = target.parent
    if sys.platform.startswith("darwin"):
        cmd = ["open", str(target)]
    elif os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    else:
        cmd = ["xdg-open", str(target)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    except OSError as exc:
        raise YtdlpError(f"无法打开目录：{exc}") from exc


# ---------------------------------------------------------------- 参数构建


def network_args(settings: dict[str, Any]) -> list[str]:
    """代理与 TLS 参数（下载与解析共用）。

    - ``auto_proxy`` 开启：完全不传 ``--proxy``，由 yt-dlp 自己读取环境变量；
    - ``auto_proxy`` 关闭：总是传 ``--proxy``，值为空即 ``--proxy ""`` 强制直连。
    """
    args: list[str] = []
    if not settings.get("auto_proxy", True):
        args += ["--proxy", str(settings.get("proxy", "") or "").strip()]
    # 使用系统证书库（可识别 302 等工具装进系统的自签名根证书）
    if settings.get("use_system_certs"):
        args += ["--compat-options", "no-certifi"]
    # 完全跳过 HTTPS 证书校验（谨慎使用）
    if settings.get("no_check_certificates"):
        args.append("--no-check-certificates")
    return args


def _auth_args(settings: dict[str, Any]) -> list[str]:
    args: list[str] = []
    cookies_file = str(settings.get("cookies_file", "")).strip()
    browser = str(settings.get("cookies_from_browser", "")).strip()
    if cookies_file:
        args += ["--cookies", str(Path(cookies_file).expanduser())]
    elif browser:
        args += ["--cookies-from-browser", browser]
    args += network_args(settings)
    return args


def effective_settings(task: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """任务级覆盖（高级选项）叠加到全局设置。"""
    merged = dict(settings)
    overrides = task.get("overrides") or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if value not in ("", None):
                merged[key] = value
    return merged


def build_args(task: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    """按 PLAN §4.1 构建完整命令行。task 为普通 dict（Task.to_dict()）。"""
    conf = effective_settings(task, settings)
    args: list[str] = [ytdlp_binary(conf), "--color", "never", "--newline", "--no-warnings", "--progress"]
    if conf.get("ignore_ytdlp_config", True):
        args.append("--ignore-config")
    args += [
        "--progress-template",
        "download:PROG %(progress)j",
        "--progress-template",
        "postprocess:PP %(progress)j",
        "--print",
        "after_move:FINAL %(filepath)s",
        "--no-simulate",
        "-P",
        str(Path(str(conf.get("download_dir", "~/Downloads/yt-dlp"))).expanduser()),
        "-o",
        str(conf.get("filename_template", "%(title)s [%(id)s].%(ext)s")),
    ]

    preset = str(task.get("preset") or conf.get("default_preset", "best"))
    if preset not in PRESET_KEYS:
        preset = "best"
    format_id = str(task.get("format_id") or "").strip()
    fmt = format_id or PRESETS[preset]["format"] or "bv*+ba/b"
    if fmt:
        args += ["-f", fmt]

    audio_format = PRESETS[preset]["audio_format"]
    if preset in AUDIO_PRESETS:
        args.append("-x")
        if audio_format:
            args += ["--audio-format", audio_format]
        args += ["--audio-quality", "0"]

    container = str(task.get("container") or conf.get("merge_container", "auto"))
    if container in ("mp4", "mkv"):
        args += ["--merge-output-format", container]

    try:
        fragments = int(conf.get("concurrent_fragments", 0) or 0)
    except (TypeError, ValueError):
        fragments = 0
    if fragments > 0:
        args += ["-N", str(fragments)]

    rate_limit = str(conf.get("rate_limit", "")).strip()
    if rate_limit:
        args += ["-r", rate_limit]

    try:
        retries = int(conf.get("retries", 10))
    except (TypeError, ValueError):
        retries = 10
    args += ["-R", str(retries)]

    args += _auth_args(conf)

    sub_langs = str(conf.get("sub_langs", "")).strip()
    if sub_langs:
        args += ["--write-subs", "--sub-langs", sub_langs]
        if conf.get("embed_subs"):
            args.append("--embed-subs")
    if conf.get("embed_metadata"):
        args.append("--embed-metadata")
    if conf.get("embed_thumbnail"):
        args.append("--embed-thumbnail")

    sponsorblock = str(conf.get("sponsorblock_remove", "")).strip()
    if sponsorblock:
        args += ["--sponsorblock-remove", sponsorblock]

    playlist_items = str(task.get("playlist_items") or "").strip()
    if task.get("is_playlist"):
        args.append("--yes-playlist")
        if playlist_items:
            args += ["-I", playlist_items]
    else:
        args.append("--no-playlist")

    args.append(str(task.get("url", "")))
    return args


# ---------------------------------------------------------------- 链接解析


async def probe(url: str, settings: dict[str, Any]) -> ProbeResult:
    """解析链接信息（视频或播放列表）。"""
    binary = ytdlp_binary(settings)
    args = [binary, "-J", "--flat-playlist", "--playlist-end", "200", "--no-warnings", "--color", "never"]
    if settings.get("ignore_ytdlp_config", True):
        # 与下载路径保持一致：否则用户 yt-dlp.conf 里的 --no-playlist 等会改变解析结果
        args.append("--ignore-config")
    args += _auth_args(settings)
    args.append(url)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), 9)
        with contextlib.suppress(OSError):
            proc.kill()
        raise YtdlpError("解析超时（60 秒），请检查网络或代理设置") from None
    except asyncio.CancelledError:
        # 用户中途返回 / 关闭页面：连同子进程一起收掉，避免孤儿进程
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), 9)
        with contextlib.suppress(OSError):
            proc.kill()
        raise

    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = strip_ansi(err.decode("utf-8", errors="replace").strip())
    if proc.returncode != 0 or not stdout:
        hint = classify_error(stderr) or classify_error(stdout)
        if hint:
            raise YtdlpError(hint)
        tail = (stderr or stdout).splitlines()[-3:]
        raise YtdlpError("解析失败：" + (" / ".join(tail) if tail else "未知错误"))

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise YtdlpError(f"解析结果不是合法 JSON：{exc}") from exc
    return normalize_probe(data, url)


def normalize_probe(data: dict[str, Any], url: str) -> ProbeResult:
    entries_raw = data.get("entries")
    is_playlist = data.get("_type") == "playlist" or isinstance(entries_raw, list)
    result = ProbeResult(
        kind="playlist" if is_playlist else "video",
        title=str(data.get("title") or data.get("fulltitle") or url),
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        duration=float(data.get("duration") or 0.0),
        thumbnail=str(data.get("thumbnail") or ""),
        webpage_url=str(data.get("webpage_url") or data.get("url") or url),
        video_id=str(data.get("id") or ""),
    )
    if is_playlist:
        entries = []
        for item in entries_raw or []:
            if not isinstance(item, dict):
                continue
            entry_url = str(item.get("webpage_url") or item.get("url") or "")
            if not entry_url:
                continue
            entries.append(
                {
                    "title": str(item.get("title") or item.get("id") or entry_url),
                    "url": entry_url,
                    "id": str(item.get("id") or ""),
                }
            )
        result.entries = entries
        try:
            result.playlist_count = int(data.get("playlist_count") or len(entries))
        except (TypeError, ValueError):
            result.playlist_count = len(entries)
    else:
        result.formats = normalize_formats(data.get("formats") or [])
    return result


def normalize_formats(raw: list[Any]) -> list[dict[str, Any]]:
    """过滤无效格式并按分辨率 / 码率倒序。"""
    formats: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("vcodec") == "none" and item.get("acodec") == "none":
            continue
        formats.append(
            {
                "format_id": str(item.get("format_id") or ""),
                "ext": str(item.get("ext") or ""),
                "resolution": str(item.get("resolution") or item.get("format_note") or ""),
                "height": int(item.get("height") or 0),
                "fps": item.get("fps") or 0,
                "vcodec": str(item.get("vcodec") or ""),
                "acodec": str(item.get("acodec") or ""),
                "filesize": int(item.get("filesize") or item.get("filesize_approx") or 0),
                "tbr": float(item.get("tbr") or 0.0),
                "note": str(item.get("format_note") or ""),
            }
        )
    formats.sort(key=lambda f: (f["height"], f["tbr"]), reverse=True)
    return formats


# ---------------------------------------------------------------- 输出行解析


def parse_output_line(line: str) -> tuple[str, Any]:
    """解析子进程单行输出。

    返回 (kind, payload)：``progress`` / ``postprocess`` 的 payload 为 dict，
    ``final`` 为文件路径，``log`` 为去除 ANSI 后的原文。
    """
    clean = strip_ansi(line).rstrip()
    if clean.startswith("PROG "):
        data = _loads(clean[5:])
        if data is not None:
            return "progress", data
    elif clean.startswith("PP "):
        data = _loads(clean[3:])
        if data is not None:
            return "postprocess", data
    elif clean.startswith("FINAL "):
        return "final", clean[6:].strip()
    return "log", clean


def _loads(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def progress_percent(data: dict[str, Any]) -> float:
    """从 progress 字典取百分比（0-100）。"""
    raw = data.get("_percent")
    if isinstance(raw, (int, float)):
        return max(0.0, min(100.0, float(raw)))
    downloaded = data.get("downloaded_bytes")
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return max(0.0, min(100.0, downloaded / total * 100))
    if data.get("status") == "finished":
        return 100.0
    return 0.0


# ---------------------------------------------------------------- 错误归类


ERROR_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("certificate verify failed", "ssl: certificate_verify_failed", "certificate has expired"),
        "HTTPS 证书校验失败：若使用中间人代理（如 Steamcommunity302），在设置里开启「使用系统证书库」或「跳过证书校验」",
    ),
    (
        ("sign in to confirm", "confirm you're not a bot", "cookies", "login required", "use --cookies"),
        "需要登录：在设置里配置 cookies 文件或浏览器 cookies",
    ),
    (
        ("http error 403", "http error 429", "too many requests"),
        "被限流或拒绝：降低并发、配置 cookies 或代理后重试",
    ),
    (
        ("requested format is not available", "requested format not available"),
        "该格式不可用：改用「最佳画质」或从格式列表重新选择",
    ),
    (("ffmpeg not found", "ffprobe", "ffmpeg is not installed"), "缺少 ffmpeg：安装后重试"),
    (
        ("postprocessing", "post-process", "embedding"),
        "后处理失败：检查 ffmpeg 是否可用，或关闭嵌入元数据 / 封面后重试",
    ),
    (("unsupported url",), "链接不受支持：检查链接或更新 yt-dlp"),
    (
        ("unable to download webpage", "timed out", "connection", "temporary failure", "getaddrinfo"),
        "网络问题：检查代理 / 网络后重试",
    ),
    (("video unavailable", "private video", "this video is private"), "视频不可用或私有：确认链接与登录状态"),
    (("disk is full", "no space left"), "磁盘空间不足：清理后重试"),
]


def classify_error(text: str) -> str:
    """把 yt-dlp 报错文本归类为中文提示；无法归类返回空串。"""
    low = strip_ansi(text or "").lower()
    if not low:
        return ""
    for needles, hint in ERROR_HINTS:
        if any(needle in low for needle in needles):
            return hint
    return ""


# ---------------------------------------------------------------- 展示格式


def human_bytes(value: float | int | None) -> str:
    size = float(value or 0)
    if size <= 0:
        return "—"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} B"
    return f"{size:.2f} {units[idx]}"


def human_speed(value: float | int | None) -> str:
    if not value:
        return "—"
    return human_bytes(value) + "/s"


def human_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    if total <= 0:
        return "—"
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def human_duration(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "—"
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def progress_bar(percent: float, width: int) -> str:
    """文本进度条（固定宽度，避免表格抖动）。"""
    width = max(4, width)
    ratio = max(0.0, min(100.0, percent)) / 100.0
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def render_filename(template: str, title: str, video_id: str = "", uploader: str = "", ext: str = "mp4") -> str:
    """仅做演示级占位符替换（不实现 yt-dlp 模板引擎）。"""
    name = template or "%(title)s.%(ext)s"
    name = name.replace("%(title)s", title or "未命名")
    name = name.replace("%(id)s", video_id or "id")
    name = name.replace("%(uploader)s", uploader or "uploader")
    name = name.replace("%(ext)s", ext)
    return name


def preview_path(settings: dict[str, Any], task: dict[str, Any], title: str, video_id: str = "", uploader: str = "") -> str:
    preset = str(task.get("preset") or settings.get("default_preset", "best"))
    container = str(task.get("container") or settings.get("merge_container", "auto"))
    if preset in AUDIO_PRESETS:
        ext = PRESETS[preset]["audio_format"] or "m4a"
    elif container in ("mp4", "mkv"):
        ext = container
    else:
        ext = "mp4"
    name = render_filename(
        str(settings.get("filename_template", "%(title)s [%(id)s].%(ext)s")),
        title,
        video_id,
        uploader,
        ext,
    )
    return str(Path(str(settings.get("download_dir", "~/Downloads/yt-dlp"))).expanduser() / name)
