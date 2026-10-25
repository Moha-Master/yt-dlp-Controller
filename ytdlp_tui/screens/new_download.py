"""新建下载屏：链接解析、信息面板、播放列表范围、格式预设与高级选项。"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Select, Static

from ..config import PRESETS
from ..ui import PageScreen
from ..widgets import STYLE_DIM, STYLE_WARN, FormField, FormModal, InputModal, shorten
from ..ytdlp import (
    ProbeResult,
    YtdlpError,
    human_duration,
    open_directory,
    preview_path,
    probe,
)
from .modals import FormatsModal, PlaylistModal

PRESET_OPTIONS = [(spec["label"], key) for key, spec in PRESETS.items()]
CONTAINER_OPTIONS = [("自动（按源）", "auto"), ("MP4", "mp4"), ("MKV", "mkv")]
AUDIO_OPTIONS = [("自动", ""), ("MP3", "mp3"), ("M4A", "m4a")]
PLAYLIST_MODES = [("全部下载", "all"), ("仅当前", "single"), ("前 N 个…", "range")]


def split_urls(text: str) -> list[str]:
    """按空格 / 逗号 / 换行拆分并去重（保持顺序）。"""
    parts = [part for part in re.split(r"[\s,]+", text.strip()) if part]
    seen: set[str] = set()
    urls: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            urls.append(part)
    return urls


class NewDownloadScreen(PageScreen):
    """粘贴链接 → 解析 → 选预设 / 范围 → 加入队列。"""

    TITLE = "新建下载"
    HINT = "Enter 解析 · Ctrl+Enter 默认预设加入 · Ctrl+V 粘贴 · Tab 轮切 · Esc/Ctrl+C 返回"

    BINDINGS = [Binding("ctrl+enter", "quick_add", "快速加入", show=False)]

    DEFAULT_CSS = """
    NewDownloadScreen #nd-preset { width: 26; }
    NewDownloadScreen #nd-container { width: 10; }
    NewDownloadScreen #nd-audio { width: 10; }
    NewDownloadScreen #nd-pl-mode { width: 16; }
    NewDownloadScreen .nd-note { height: 1; color: $text-muted; }
    NewDownloadScreen #nd-pl-box { height: auto; }
    NewDownloadScreen .nd-pl-line { height: 1; color: $warning; }
    NewDownloadScreen #nd-output {
        height: 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    NewDownloadScreen .kv-value.nw {
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._urls: list[str] = []
        self._result: ProbeResult | None = None
        self._probed_url = ""
        self._preset = "best"
        self._format_id = ""
        self._container = "auto"
        self._playlist_mode = "all"
        self._playlist_items = ""
        self._overrides: dict[str, Any] = {}
        self._busy = False
        self._initializing = False
        self.menu = [
            ("查看全部格式…", self._open_formats, False),
            ("高级选项…", self._open_advanced, False),
            ("打开下载目录", self._open_download_dir, False),
        ]

    # ------------------------------------------------------------ 组合

    def compose_toolbar(self):
        yield Button("粘贴并解析", id="nd-paste")

    def compose_page(self):
        with Vertical(classes="panel tight"):
            with Horizontal(classes="filter-row"):
                yield Static("链接", classes="fl-label")
                yield Input(
                    placeholder="粘贴视频或播放列表链接，可空格 / 逗号分隔多条",
                    compact=True,
                    id="nd-url",
                )
                yield Button("解析", id="nd-parse", variant="primary")
            yield Static(
                "支持视频、播放列表；播放列表会先列出条目再决定范围",
                id="nd-note",
                classes="nd-note",
            )

        with Vertical(classes="panel tight"):
            with Horizontal(classes="kv-row"):
                yield Static("标题", classes="kv-label sm")
                yield Static("未解析", classes="kv-value nw", id="nd-title")
            with Horizontal(classes="kv-row"):
                yield Static("上传者", classes="kv-label sm")
                yield Static("—", classes="kv-value nw", id="nd-uploader")
            with Horizontal(classes="kv-row"):
                yield Static("时长", classes="kv-label sm")
                yield Static("—", classes="kv-value nw", id="nd-duration")
            with Vertical(id="nd-pl-box"):
                yield Static("", id="nd-pl-summary", classes="nd-pl-line")
                with Horizontal(classes="filter-row"):
                    yield Select(
                        PLAYLIST_MODES,
                        value="all",
                        allow_blank=False,
                        compact=True,
                        id="nd-pl-mode",
                    )
                    yield Button("查看条目", id="nd-pl-view")
                    yield Static(classes="fill")

        with Vertical(classes="panel tight"):
            with Horizontal(classes="opt-row"):
                yield Static("预设", classes="opt-label")
                yield Select(PRESET_OPTIONS, value="best", allow_blank=False, compact=True, id="nd-preset")
                yield Static("容器", classes="opt-label")
                yield Select(CONTAINER_OPTIONS, value="auto", allow_blank=False, compact=True, id="nd-container")
                yield Static("音频格式", classes="opt-label")
                yield Select(AUDIO_OPTIONS, value="", allow_blank=False, compact=True, id="nd-audio")
            with Horizontal(classes="filter-row gap-top"):
                yield Button("查看全部格式…", id="nd-formats")
                yield Button("高级选项…", id="nd-advanced")
                yield Static(classes="fill")
            yield Static("输出路径：解析后显示", id="nd-output")

        with Horizontal(classes="filter-row gap-top"):
            yield Static(classes="fill")
            yield Button("取消", id="nd-cancel")
            yield Button("加入队列", id="nd-add", variant="primary")

    def on_mount(self) -> None:
        settings = self.app.get_settings()
        self._preset = str(settings.get("default_preset", "best"))
        self._container = str(settings.get("merge_container", "auto"))
        self._initializing = True
        try:
            self.query_one("#nd-preset", Select).value = self._preset
            self.query_one("#nd-container", Select).value = self._container
            self.query_one("#nd-audio", Select).value = self._audio_value()
            self.query_one("#nd-pl-box").display = False
        finally:
            self._initializing = False
        self.set_subtitle(f"预设：{PRESETS.get(self._preset, PRESETS['best'])['label']}")
        self._refresh_preview()
        self.query_one("#nd-url", Input).focus()

    # ------------------------------------------------------------ 解析

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "nd-url":
            self.action_parse()

    def action_parse(self) -> None:
        if self._busy:
            return
        urls = split_urls(self.query_one("#nd-url", Input).value)
        if not urls:
            self.app.notify_warn("请先粘贴至少一条链接")
            self.query_one("#nd-url", Input).focus()
            return
        self._urls = urls
        self._set_busy(True, "正在解析链接…")
        self._probe(urls[0])

    @work(exclusive=True)
    async def _probe(self, url: str) -> None:
        error = ""
        result: ProbeResult | None = None
        try:
            result = await probe(url, self.app.get_settings())
        except YtdlpError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"解析失败：{type(exc).__name__}: {exc}"
        self._set_busy(False)
        if result is None:
            self.app.notify_err(error or "解析失败：未返回结果")
            return
        self._apply_probe(url, result)

    def _apply_probe(self, url: str, result: ProbeResult) -> None:
        self._result = result
        self._probed_url = url
        self.query_one("#nd-title", Static).update(Text(shorten(result.title, 120)))
        self.query_one("#nd-uploader", Static).update(Text(result.uploader or "—"))
        self.query_one("#nd-duration", Static).update(Text(human_duration(result.duration)))
        note = "支持视频、播放列表；播放列表会先列出条目再决定范围"
        if len(self._urls) > 1:
            note = (
                f"已解析第一条；另有 {len(self._urls) - 1} 条链接将使用相同预设入队"
                "（选项面板只反映第一条）"
            )
        self.query_one("#nd-note", Static).update(Text(note, style=STYLE_DIM if len(self._urls) > 1 else ""))

        if result.is_playlist:
            self._playlist_mode = "all"
            self._playlist_items = ""
            self.query_one("#nd-pl-box").display = True
            self.query_one("#nd-pl-mode", Select).value = "all"
            self._refresh_playlist_summary()
        else:
            self.query_one("#nd-pl-box").display = False
            self._playlist_items = ""
        self._refresh_preview()
        self.app.status(f"已解析：{result.title}", "ok")

    def _refresh_playlist_summary(self) -> None:
        result = self._result
        if result is None:
            return
        count = result.entry_count
        if self._playlist_mode == "all":
            scope = f"全部（1-{count}）" if count else "全部"
        elif self._playlist_mode == "single":
            scope = "仅当前（第 1 个）"
        else:
            count_text = self._playlist_items.split("-")[-1]
            scope = f"前 {count_text} 个（{self._playlist_items}）"
        self.query_one("#nd-pl-summary", Static).update(
            Text(f"播放列表：检测到 {count} 个视频 · 范围：{scope}", style=STYLE_WARN)
        )

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for button_id in ("#nd-parse", "#nd-add", "#nd-paste"):
            try:
                self.query_one(button_id, Button).disabled = busy
            except Exception:
                continue
        if busy:
            self.app.spinner(True, message)
        else:
            self.app.spinner(False)
            self.app.status("就绪")

    # ------------------------------------------------------------ 选项

    def _audio_value(self) -> str:
        return PRESETS.get(self._preset, {}).get("audio_format", "")

    @on(Select.Changed, "#nd-preset")
    def _on_preset(self, event: Select.Changed) -> None:
        if self._initializing or event.value is Select.NULL:
            return
        self._preset = str(event.value)
        if self._preset != "custom":
            self._format_id = ""
        self._initializing = True
        try:
            self.query_one("#nd-audio", Select).value = self._audio_value()
        finally:
            self._initializing = False
        self.set_subtitle(f"预设：{PRESETS.get(self._preset, PRESETS['best'])['label']}")
        self._refresh_preview()

    @on(Select.Changed, "#nd-container")
    def _on_container(self, event: Select.Changed) -> None:
        if self._initializing or event.value is Select.NULL:
            return
        self._container = str(event.value)
        self._refresh_preview()

    @on(Select.Changed, "#nd-audio")
    def _on_audio(self, event: Select.Changed) -> None:
        if self._initializing or event.value is Select.NULL:
            return
        value = str(event.value)
        if value == "mp3":
            preset = "audio_mp3"
        elif value == "m4a":
            preset = "audio_m4a"
        elif self._preset in ("audio_mp3", "audio_m4a"):
            preset = "audio_best"
        else:
            preset = self._preset
        if preset != self._preset:
            self._preset = preset
            self._format_id = ""
            self._initializing = True
            try:
                self.query_one("#nd-preset", Select).value = preset
            finally:
                self._initializing = False
            self.set_subtitle(f"预设：{PRESETS[preset]['label']}")
        self._refresh_preview()

    @on(Select.Changed, "#nd-pl-mode")
    def _on_playlist_mode(self, event: Select.Changed) -> None:
        if self._initializing or event.value is Select.NULL:
            return
        mode = str(event.value)
        if mode == "range":
            self._ask_range()
            return
        self._playlist_mode = mode
        self._playlist_items = ""
        self._refresh_playlist_summary()

    def _ask_range(self) -> None:
        result = self._result
        count = result.entry_count if result else 0
        limit = max(1, count)

        def validator(value: str) -> bool:
            try:
                number = int(value)
            except ValueError:
                return False
            return 1 <= number <= limit

        default = str(min(10, limit))
        self.app.push_screen(
            InputModal(
                f"播放列表共 {count} 个条目，请输入要下载的数量（1-{limit}）：",
                title="下载前 N 个",
                value=default,
                validator=validator,
                error=f"请输入 1-{limit} 之间的整数",
                hint="范围将写为 playlist_items=1-N",
            ),
            self._on_range_result,
        )

    def _on_range_result(self, value: str | None) -> None:
        if value is None:
            self._initializing = True
            try:
                self.query_one("#nd-pl-mode", Select).value = self._playlist_mode
            finally:
                self._initializing = False
            return
        self._playlist_mode = "range"
        self._playlist_items = f"1-{int(value)}"
        self._refresh_playlist_summary()

    def _refresh_preview(self) -> None:
        settings = self.app.get_settings()
        title = self._result.title if self._result else ""
        task = {"preset": self._preset, "container": self._container, "format_id": self._format_id}
        if self._result:
            path = preview_path(
                settings,
                task,
                title,
                self._result.video_id,
                self._result.uploader,
            )
            self.query_one("#nd-output", Static).update(Text(f"输出路径：{path}"))
        else:
            directory = str(settings.get("download_dir", ""))
            self.query_one("#nd-output", Static).update(Text(f"输出路径：{directory}/（解析后显示文件名）"))

    # ------------------------------------------------------------ 格式 / 高级

    def _open_formats(self) -> None:
        if self._result is None:
            self.app.notify_warn("请先解析链接")
            return
        if self._result.is_playlist or not self._result.formats:
            self.app.notify_warn("播放列表或直链没有可选格式，请直接使用预设")
            return
        self.app.push_screen(FormatsModal(self._result.formats), self._on_format_picked)

    def _on_format_picked(self, format_id: str | None) -> None:
        if not format_id:
            return
        self._format_id = format_id
        self._preset = "custom"
        self._initializing = True
        try:
            self.query_one("#nd-preset", Select).value = "custom"
        finally:
            self._initializing = False
        self.set_subtitle(f"预设：自定义格式 {format_id}")
        self._refresh_preview()
        self.app.notify_ok(f"已选择格式 {format_id}")

    def _open_advanced(self) -> None:
        settings = self.app.get_settings()
        fields = [
            FormField("sub_langs", "字幕语言", value=str(settings.get("sub_langs", "")), placeholder="如 zh.*,en.*"),
            FormField(
                "embed_subs",
                "嵌入字幕",
                kind="switch",
                value="on" if settings.get("embed_subs") else "",
            ),
            FormField(
                "embed_metadata",
                "嵌入元数据",
                kind="switch",
                value="on" if settings.get("embed_metadata") else "",
            ),
            FormField(
                "embed_thumbnail",
                "嵌入封面",
                kind="switch",
                value="on" if settings.get("embed_thumbnail") else "",
            ),
            FormField(
                "sponsorblock_remove",
                "SponsorBlock",
                value=str(settings.get("sponsorblock_remove", "")),
                placeholder="如 sponsor，空=关闭",
            ),
            FormField(
                "rate_limit",
                "限速",
                value=str(settings.get("rate_limit", "")),
                placeholder="如 5M，空=继承设置",
            ),
            FormField(
                "playlist_items",
                "播放列表条目",
                value=self._playlist_items,
                placeholder="如 1-10，空=全部",
            ),
        ]
        self.app.push_screen(
            FormModal("高级选项（仅本次任务）", fields, message="留空或与全局一致表示继承设置"),
            self._on_advanced_done,
        )

    def _on_advanced_done(self, data: dict | None) -> None:
        if not data:
            return
        settings = self.app.get_settings()
        overrides: dict[str, Any] = {}
        for key, value in data.items():
            if key == "playlist_items":
                self._playlist_items = str(value or "")
                continue
            if value in ("", None):
                continue
            if value == settings.get(key):
                continue
            overrides[key] = value
        self._overrides = overrides
        self.app.notify_ok("高级选项已应用到本次任务")

    # ------------------------------------------------------------ 加入队列

    def _on_paste(self) -> None:
        text = (self.app.clipboard or "").strip()
        if not text:
            self.app.notify_warn("无法读取系统剪贴板，请在输入框按 Ctrl+V 粘贴后回车解析")
            self.query_one("#nd-url", Input).focus()
            return
        self.query_one("#nd-url", Input).value = text
        self.action_parse()

    def action_quick_add(self) -> None:
        """Ctrl+Enter：跳过格式选择，用默认预设直接入队。"""
        settings = self.app.get_settings()
        self._preset = str(settings.get("default_preset", "best"))
        self._format_id = ""
        self._container = str(settings.get("merge_container", "auto"))
        self.action_add()

    def action_add(self) -> None:
        if self._busy:
            return
        text = self.query_one("#nd-url", Input).value
        urls = split_urls(text) if text.strip() else list(self._urls)
        if not urls:
            self.app.notify_warn("请先粘贴至少一条链接")
            self.query_one("#nd-url", Input).focus()
            return

        queue = self.app.get_queue()
        for index, url in enumerate(urls):
            result = self._result if (index == 0 and url == self._probed_url) else None
            is_playlist = bool(result and result.is_playlist)
            title = result.title if result else ""
            uploader = result.uploader if result else ""
            duration = result.duration if result else 0.0
            if is_playlist and self._playlist_mode == "single":
                is_playlist = False
            items = self._playlist_items if is_playlist else ""
            queue.add(
                url=url,
                title=title,
                uploader=uploader,
                duration=duration,
                preset=self._preset,
                format_id=self._format_id,
                container=self._container,
                playlist_items=items,
                is_playlist=is_playlist,
                overrides=self._overrides,
            )

        self.app.notify_ok(f"已加入队列（{len(urls)} 个任务）")
        if self.app.get_settings().get("open_queue_after_add", True):
            from .queue import QueueScreen

            previous = self.app.screen_stack[-2] if len(self.app.screen_stack) > 1 else None
            if isinstance(previous, QueueScreen):
                self.app.pop_screen()
            else:
                self.app.switch_screen(QueueScreen())

    # ------------------------------------------------------------ 其它

    def _open_download_dir(self) -> None:
        target = str(self.app.get_settings().get("download_dir", ""))
        try:
            open_directory(target)
        except YtdlpError as exc:
            self.app.notify_warn(str(exc))

    @on(Button.Pressed, "#nd-parse")
    def _on_parse(self) -> None:
        self.action_parse()

    @on(Button.Pressed, "#nd-add")
    def _on_add(self) -> None:
        self.action_add()

    @on(Button.Pressed, "#nd-paste")
    def _on_paste_button(self) -> None:
        self._on_paste()

    @on(Button.Pressed, "#nd-cancel")
    def _on_cancel(self) -> None:
        self.action_page_back()

    @on(Button.Pressed, "#nd-formats")
    def _on_formats_button(self) -> None:
        self._open_formats()

    @on(Button.Pressed, "#nd-advanced")
    def _on_advanced_button(self) -> None:
        self._open_advanced()

    @on(Button.Pressed, "#nd-pl-view")
    def _on_playlist_view(self) -> None:
        if self._result is None or not self._result.is_playlist:
            self.app.notify_warn("请先解析播放列表链接")
            return
        self.app.push_screen(
            PlaylistModal(
                f"播放列表：{self._result.title}",
                self._result.entries[:20],
                total=self._result.entry_count,
            )
        )


__all__ = ["NewDownloadScreen", "split_urls"]
