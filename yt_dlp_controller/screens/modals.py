"""业务模态：格式选择、播放列表条目、任务详情（sheet 式）。"""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static, TextArea

from ..queue import STATUS_LABELS
from ..widgets import (
    STATUS_COLORS,
    STYLE_DIM,
    STYLE_ERR,
    fit_table_columns,
    load_rows,
    make_table,
    rcell,
    shorten,
)
from ..ytdlp import human_bytes, human_duration, human_eta, human_speed

if TYPE_CHECKING:
    from ..queue import QueueManager


class FormatsModal(ModalScreen["str | None"]):
    """格式列表：Enter / 单击选择，返回格式 ID；None=取消。"""

    DEFAULT_CSS = """
    FormatsModal { align: center middle; }
    FormatsModal > .modal-box { width: 90%; max-width: 104; height: 88%; }
    FormatsModal .fmt-hint { height: 1; padding: 0 1; color: $text-muted; }
    FormatsModal .fmt-body { height: 1fr; padding: 0 1 1 1; }
    FormatsModal .fmt-body .tbl { margin: 0; height: 1fr; }
    """

    BINDINGS = [Binding("escape,ctrl+c", "cancel", show=False)]

    def __init__(self, formats: list[dict[str, Any]], title: str = "可用格式") -> None:
        super().__init__()
        self.formats = formats
        self.title_text = title

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            yield Static("↑↓ 选择 · 单击/回车 使用该格式 · Esc 取消", classes="fmt-hint")
            with Vertical(classes="fmt-body"):
                yield make_table(
                    "ID", "格式", "分辨率", "帧率", "编码", "大小", "码率", id="fmt-table", activate="click"
                )
            with Horizontal(classes="btn-row"):
                yield Button("关闭", id="fmt-close")
                yield Button("使用所选格式", id="fmt-ok", variant="primary")

    def on_mount(self) -> None:
        # 首帧 region 尚未确定，等布局稳定后再按真实宽度分列
        self.call_after_refresh(self._fill)

    def on_resize(self, event) -> None:
        self._fill()

    def _fill(self) -> None:
        try:
            table = self.query_one("#fmt-table", DataTable)
        except Exception:
            return
        vis = fit_table_columns(table, [8, 14, 12, 6, 16, 10, 10], rows=len(self.formats))
        keys = list(table.columns.keys())
        table.columns[keys[3]].label = rcell("帧率", vis[3] - 2)
        table.columns[keys[5]].label = rcell("大小", vis[5] - 2)
        table.columns[keys[6]].label = rcell("码率", vis[6] - 2)
        rows = []
        row_keys: list[str] = []
        for fmt in self.formats:
            codec = " / ".join(
                part.split(".")[0]
                for part in (fmt.get("vcodec", ""), fmt.get("acodec", ""))
                if part and part != "none"
            )
            label = fmt.get("ext", "") or ""
            if fmt.get("note"):
                label = f"{label} {fmt['note']}".strip()
            rows.append(
                [
                    Text(shorten(fmt.get("format_id", ""), vis[0] - 2)),
                    Text(shorten(label, vis[1] - 2)),
                    Text(shorten(fmt.get("resolution", "") or "—", vis[2] - 2)),
                    rcell(fmt.get("fps") or "—", vis[3] - 2),
                    Text(shorten(codec or "—", vis[4] - 2)),
                    rcell(human_bytes(fmt.get("filesize")), vis[5] - 2),
                    rcell(f"{float(fmt.get('tbr') or 0):.0f}k" if fmt.get("tbr") else "—", vis[6] - 2),
                ]
            )
            row_keys.append(str(fmt.get("format_id", "")))
        table.clear()
        for cells, key in zip(rows, row_keys, strict=True):
            table.add_row(*cells, key=key or None)
        table.focus()
        if rows:
            table.move_cursor(row=0, column=0)

    def _selected(self) -> str | None:
        table = self.query_one("#fmt-table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        return str(key) if key is not None else None

    @on(DataTable.RowSelected, "#fmt-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        value = event.row_key.value
        self.dismiss(str(value) if value is not None else None)

    @on(Button.Pressed, "#fmt-ok")
    def _on_ok(self) -> None:
        self.dismiss(self._selected())

    @on(Button.Pressed, "#fmt-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlaylistModal(ModalScreen[None]):
    """播放列表条目预览（前 200 条）。"""

    DEFAULT_CSS = """
    PlaylistModal { align: center middle; }
    PlaylistModal > .modal-box { width: 90%; max-width: 100; height: 88%; }
    PlaylistModal .pl-hint { height: 1; padding: 0 1; color: $text-muted; }
    PlaylistModal .pl-body { height: 1fr; padding: 0 1 1 1; }
    PlaylistModal .pl-body .tbl { margin: 0; height: 1fr; }
    """

    BINDINGS = [Binding("escape,ctrl+c", "close", show=False)]

    def __init__(self, title: str, entries: list[dict[str, Any]], total: int = 0) -> None:
        super().__init__()
        self.title_text = title
        self.entries = entries
        self.total = total or len(entries)

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            yield Static(f"共 {self.total} 个条目，列出前 {len(self.entries)} 个 · Esc 关闭", classes="pl-hint")
            with Vertical(classes="pl-body"):
                yield make_table("序号", "标题", "链接", id="pl-table")
            with Horizontal(classes="btn-row"):
                yield Button("关闭", id="pl-close", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(self._fill)

    def on_resize(self, event) -> None:
        self._fill()

    def _fill(self) -> None:
        try:
            table = self.query_one("#pl-table", DataTable)
        except Exception:
            return
        vis = fit_table_columns(table, [6, 46, 40], rows=len(self.entries))
        keys = list(table.columns.keys())
        table.columns[keys[0]].label = rcell("序号", vis[0] - 2)
        rows = []
        for idx, entry in enumerate(self.entries):
            rows.append(
                [
                    rcell(idx + 1, vis[0] - 2),
                    Text(shorten(entry.get("title", ""), vis[1] - 2)),
                    Text(shorten(entry.get("url", ""), vis[2] - 2), style=STYLE_DIM),
                ]
            )
        load_rows(table, rows)
        table.focus()

    @on(Button.Pressed, "#pl-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class TaskDetailModal(ModalScreen["str | None"]):
    """任务详情：kv 摘要 + 完整命令 + 日志尾部。返回 "retry" 表示请求重试。"""

    DEFAULT_CSS = """
    TaskDetailModal { align: center middle; }
    TaskDetailModal > .modal-box { width: 90%; max-width: 104; height: 88%; }
    TaskDetailModal .det-body { height: 1fr; padding: 1 2; }
    TaskDetailModal .det-cmd { height: 5; margin-top: 1; border: round $secondary; }
    TaskDetailModal .det-log { height: 12; margin-top: 1; border: round $secondary; }
    TaskDetailModal .det-sec { height: 1; color: $text-muted; margin-top: 1; }
    TaskDetailModal .det-kv { height: auto; }
    """

    BINDINGS = [Binding("escape,ctrl+c", "close", show=False)]

    def __init__(self, task: dict[str, Any], manager: QueueManager) -> None:
        super().__init__()
        self.task_data = task
        self.manager = manager

    def _snapshot(self) -> dict[str, Any]:
        task = self.manager.get(str(self.task_data.get("id", "")))
        return task.to_dict() if task is not None else self.task_data

    def compose(self):
        task = self._snapshot()
        with Vertical(classes="modal-box"):
            yield Static(Text("任务详情", style="bold"), classes="modal-title")
            with VerticalScroll(classes="det-body"):
                with Vertical(classes="det-kv"):
                    for label, value in self._summary_rows(task):
                        with Horizontal(classes="kv-row"):
                            yield Static(label, classes="kv-label sm")
                            yield Static(value, classes="kv-value")
                yield Static("命令", classes="det-sec")
                yield TextArea(
                    self._command_text(task),
                    read_only=True,
                    show_line_numbers=False,
                    soft_wrap=True,
                    classes="det-cmd",
                )
                yield Static("日志尾部", classes="det-sec")
                yield TextArea(
                    "\n".join(task.get("log_tail") or ["（暂无日志）"]),
                    read_only=True,
                    show_line_numbers=False,
                    soft_wrap=True,
                    classes="det-log",
                    id="det-log",
                )
            with Horizontal(classes="btn-row"):
                yield Button("复制命令", id="det-copy")
                yield Button("关闭", id="det-close", variant="primary")

    def _summary_rows(self, task: dict[str, Any]) -> list[tuple[str, Text]]:
        status = str(task.get("status", ""))
        status_text = Text(STATUS_LABELS.get(status, status))
        status_text.stylize(STATUS_COLORS.get(status, ""))
        raw_total = int(task.get("total_bytes") or 0)
        raw_downloaded = int(task.get("downloaded_bytes") or 0)
        total_text = human_bytes(raw_total)
        downloaded_text = human_bytes(raw_downloaded)
        if raw_total <= 0:
            size = downloaded_text
        elif raw_downloaded >= raw_total:
            size = total_text
        else:
            mark = "~" if task.get("total_estimated") else ""
            size = f"{downloaded_text} / {mark}{total_text}"
        rows: list[tuple[str, Text]] = [
            ("链接", Text(str(task.get("url", "")))),
            ("状态", status_text),
            ("输出", Text(str(task.get("filepath") or "—"))),
            ("大小", Text(size)),
            ("耗时", Text(self._elapsed(task))),
            ("速度", Text(human_speed(task.get("speed")))),
            ("剩余", Text(human_eta(task.get("eta")))),
        ]
        if task.get("overrides"):
            rows.append(("覆盖项", Text(str(task.get("overrides")))))
        if task.get("error"):
            rows.append(("错误", Text(str(task.get("error")), style=STYLE_ERR)))
        if task.get("error_hint"):
            rows.append(("提示", Text(str(task.get("error_hint")), style=STYLE_ERR)))
        return rows

    @staticmethod
    def _elapsed(task: dict[str, Any]) -> str:
        started = float(task.get("started_at") or 0)
        if not started:
            return "—"
        finished = float(task.get("finished_at") or 0) or time.time()
        return human_duration(finished - started)

    @staticmethod
    def _command_text(task: dict[str, Any]) -> str:
        cmd = task.get("cmd") or []
        if not cmd:
            return "（尚未生成命令）"
        return " ".join(shlex.quote(str(part)) for part in cmd)

    def on_mount(self) -> None:
        self.call_after_refresh(self._scroll_logs)

    def _scroll_logs(self) -> None:
        try:
            log = self.query_one("#det-log", TextArea)
        except Exception:
            return
        log.scroll_end(animate=False)

    @on(Button.Pressed, "#det-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#det-copy")
    def _on_copy(self) -> None:
        self.app.copy_to_clipboard(self._command_text(self._snapshot()))
        self.app.notify_ok("命令已复制到剪贴板")

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["FormatsModal", "PlaylistModal", "TaskDetailModal"]
