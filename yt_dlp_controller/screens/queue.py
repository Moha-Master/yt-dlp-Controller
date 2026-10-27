"""任务队列屏：一张表同时承载进行中任务与下载历史，按状态筛选、暂停 / 停止 / 重试 / 删除与详情。"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from ..config import save_config
from ..queue import (
    ACTIVE_STATUSES,
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    PAUSED,
    POSTPROCESSING,
    QUEUED,
    RUNNING,
    STATUS_LABELS,
    QueueEvent,
    QueueManager,
    Task,
)
from ..ui import PageScreen
from ..widgets import (
    STATUS_COLORS,
    STYLE_DIM,
    ClickTable,
    ConfirmModal,
    InputModal,
    filter_fuzzy,
    fit_table_columns,
    rcell,
    shorten,
)
from ..ytdlp import human_bytes_compact, human_eta, human_speed, progress_bar
from .modals import TaskDetailModal

FILTERS = [("all", "全部"), ("active", "进行中"), ("done", "已完成"), ("failed", "失败")]

WIDE_COLUMNS = [
    ("idx", "#"),
    ("title", "标题"),
    ("status", "状态"),
    ("progress", "进度"),
    ("speed", "速度"),
    ("eta", "剩余"),
    ("size", "大小"),
]
WIDE_WEIGHTS = [4, 34, 9, 16, 9, 8, 15]

NARROW_COLUMNS = [
    ("idx", "#"),
    ("title", "标题"),
    ("status", "状态"),
    ("progress", "进度"),
    ("size", "大小"),
]
NARROW_WEIGHTS = [4, 34, 9, 18, 20]

NARROW_BREAKPOINT = 96

# 排序权重：活跃任务优先，历史记录沉底（失败比已完成更靠前，便于发现异常）
STATUS_RANK = {
    RUNNING: 0,
    POSTPROCESSING: 1,
    PAUSED: 2,
    QUEUED: 3,
    INTERRUPTED: 4,
    FAILED: 5,
    COMPLETED: 6,
    CANCELLED: 7,
}

# 控制行按钮的可用条件（选中任务后才点亮）
STOPPABLE = {QUEUED, RUNNING, POSTPROCESSING, PAUSED, INTERRUPTED}
PAUSABLE = {RUNNING, PAUSED}
RETRYABLE = {INTERRUPTED, FAILED, COMPLETED, CANCELLED}
REMOVABLE = RETRYABLE | {QUEUED}


class QueueScreen(PageScreen):
    """任务队列：进行中任务与下载历史同表展示，按状态筛选区分。"""

    TITLE = "任务队列"

    BINDINGS = [
        Binding("ctrl+n", "new_download", "新建", show=False),
        Binding("ctrl+x", "stop_task", "停止", show=False),
        Binding("delete", "remove_task", "删除", show=False),
        Binding("/", "focus_search", "搜索", show=False),
    ] + ([Binding("ctrl+p", "toggle_pause", "暂停/继续", show=False)] if QueueManager.can_pause() else [])

    def __init__(self) -> None:
        super().__init__()
        self.filter_key = "all"
        self._display: list[Task] = []
        self._display_ids: list[str] = []
        self._status_cache: dict[str, str] = {}
        self._cols_key: tuple[str, ...] = ()
        self._narrow = False
        self._search = ""
        hint = (
            "↑↓ 选择 · 回车 详情 · Ctrl+N 新建 · Ctrl+P 暂停 · Ctrl+X 停止 · "
            "Del 删除 · / 搜索 · Esc 返回"
        )
        if not QueueManager.can_pause():
            hint = hint.replace("Ctrl+P 暂停 · ", "")
        self.HINT = hint

    def compose_toolbar(self):
        yield Button("⚙ 并发", id="q-conc")

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                for key, label in FILTERS:
                    yield Button(label, id=f"f-{key}", classes="q-filter")
                yield Static(classes="fill")
                yield Input(placeholder="按标题 / 链接筛选（/ 聚焦）", compact=True, id="q-search")
                yield Button("＋ 新建", id="q-new", variant="primary")
            with Horizontal(classes="filter-row gap-top"):
                if QueueManager.can_pause():
                    yield Button("暂停", id="q-pause")
                yield Button("停止", id="q-stop")
                yield Button("重试", id="q-retry")
                yield Button("删除任务", id="q-remove", variant="error")
                yield Static(classes="fill")
        yield ClickTable(cursor_type="row", zebra_stripes=True, classes="tbl", id="q-table")

    def on_mount(self) -> None:
        self._rebuild()
        # 首帧布局稳定后再算一次列宽（on_mount 时 region 仍为 0）
        self.call_after_refresh(self._rebuild)
        self.query_one("#q-table", DataTable).focus()

    # ------------------------------------------------------------ 数据 / 渲染

    def reload_page(self) -> None:
        self._rebuild()

    def _matches(self, task: Task) -> bool:
        if self.filter_key == "active" and task.status not in ACTIVE_STATUSES:
            return False
        if self.filter_key == "done" and task.status != COMPLETED:
            return False
        if self.filter_key == "failed" and task.status != FAILED:
            return False
        if self._search:
            return bool(filter_fuzzy([task], self._search, lambda t: f"{t.label} {t.url} {t.uploader}"))
        return True

    def _visible_tasks(self) -> list[Task]:
        tasks = [t for t in self.app.get_queue().tasks.values() if self._matches(t)]
        tasks.sort(key=lambda t: (STATUS_RANK.get(t.status, 99), -t.created_at))
        return tasks

    def _ensure_columns(self, table: DataTable) -> tuple[list[tuple[str, str]], list[float]]:
        narrow = self.size.width < NARROW_BREAKPOINT
        columns = NARROW_COLUMNS if narrow else WIDE_COLUMNS
        weights = NARROW_WEIGHTS if narrow else WIDE_WEIGHTS
        key = tuple(k for k, _ in columns)
        if key != self._cols_key:
            table.clear(columns=True)
            for col_key, label in columns:
                table.add_column(label, key=col_key)
            self._cols_key = key
        self._narrow = narrow
        return columns, list(weights)

    def _rebuild(self) -> None:
        # 不用 is_mounted 守卫：on_mount 期间它仍是 False（UI-SPEC §12 历史事故）
        try:
            table = self.query_one("#q-table", DataTable)
        except Exception:
            return
        cursor_id = self._current_id()
        columns, weights = self._ensure_columns(table)
        tasks = self._visible_tasks()
        self._display = tasks
        self._display_ids = [t.id for t in tasks]
        vis = fit_table_columns(table, weights, rows=len(tasks))
        labels = dict(columns)
        for idx, (col_key, _) in enumerate(columns):
            content_width = max(1, vis[idx] - 2)
            table.columns[col_key].label = rcell(labels[col_key], content_width) if col_key in {
                "idx",
                "speed",
                "eta",
                "size",
            } else labels[col_key]
        table.clear()
        for idx, task in enumerate(tasks):
            cells: list[Any] = [rcell(idx + 1, vis[0] - 2)]
            cells.append(Text(shorten(task.label, vis[1] - 2)))
            cells.append(self._status_cell(task))
            cells.append(self._progress_cell(task, vis[3] - 2))
            if self._narrow:
                cells.append(rcell(self._size_text(task), vis[4] - 2))
            else:
                live = task.status == RUNNING
                cells.append(rcell(human_speed(task.speed) if live else "—", vis[4] - 2))
                cells.append(rcell(human_eta(task.eta) if live else "—", vis[5] - 2))
                cells.append(rcell(self._size_text(task), vis[6] - 2))
            table.add_row(*cells, key=task.id)
        self._status_cache = {t.id: t.status for t in tasks}
        self._update_chrome()
        if cursor_id and cursor_id in self._display_ids:
            table.move_cursor(row=self._display_ids.index(cursor_id), column=0, animate=False)

    @staticmethod
    def _size_text(task: Task) -> str:
        """下载中显示「已下载/总量」（估算加 ~），结束后只显示总量。"""
        total = int(task.total_bytes or 0)
        downloaded = int(task.downloaded_bytes or 0)
        if total <= 0:
            return human_bytes_compact(downloaded) if downloaded > 0 else "—"
        if downloaded >= total:
            return human_bytes_compact(total)
        downloaded_text = human_bytes_compact(downloaded) if downloaded > 0 else "0B"
        mark = "~" if task.total_estimated else ""
        return f"{downloaded_text}/{mark}{human_bytes_compact(total)}"

    def _status_cell(self, task: Task) -> Text:
        text = Text(STATUS_LABELS.get(task.status, task.status))
        text.stylize(STATUS_COLORS.get(task.status, ""))
        return text

    def _progress_cell(self, task: Task, width: int) -> Text:
        width = max(6, width)
        percent = 100.0 if task.status == COMPLETED else float(task.percent or 0)
        percent = max(0.0, min(100.0, percent))
        bar_width = max(3, width - 5)
        filled = max(0, min(bar_width, int(round(percent / 100 * bar_width))))
        bar = progress_bar(percent, bar_width)
        text = Text(no_wrap=True, overflow="crop")
        text.append(bar[:filled], style=STATUS_COLORS.get(task.status, ""))
        text.append(bar[filled:], style=STYLE_DIM)
        text.append(f" {percent:3.0f}%")
        return text

    def _update_chrome(self) -> None:
        counts = self.app.get_queue().counts()
        self.set_subtitle(f"{counts['running']} 进行中 · {counts['queued']} 等待 · {counts['completed']} 已完成")
        for key, _ in FILTERS:
            try:
                self.query_one(f"#f-{key}", Button).variant = "primary" if key == self.filter_key else "default"
            except Exception:
                continue
        if not self._display:
            self.app.status("暂无任务，按 Ctrl+N 新建下载")
        else:
            self.app.status(self.app.queue_summary())
        # 按钮可用性依赖光标所在行，重建后（含 move_cursor）统一刷新
        self.call_after_refresh(self._update_controls)

    def _set_button(self, button_id: str, enabled: bool, label: str | None = None) -> None:
        try:
            button = self.query_one(f"#{button_id}", Button)
        except Exception:
            return
        button.disabled = not enabled
        if label is not None and str(button.label) != label:
            button.label = label

    def _update_controls(self) -> None:
        """控制行按钮随选中任务的状态点亮 / 置灰。"""
        task = self._current()
        status = task.status if task is not None else ""
        if QueueManager.can_pause():
            self._set_button("q-pause", status in PAUSABLE, "继续" if status == PAUSED else "暂停")
        self._set_button("q-stop", status in STOPPABLE)
        self._set_button("q-retry", status in RETRYABLE)
        self._set_button("q-remove", status in REMOVABLE)

    # ------------------------------------------------------------ 事件消费

    def apply_queue_events(self, events: list[QueueEvent]) -> None:
        """由 App 统一消费事件后批量回调（约 200ms 一批），避免每帧重建表格。"""
        if not self.is_mounted:
            return
        queue = self.app.get_queue()
        need_reload = False
        for event in events:
            if event.kind == "tasks":
                need_reload = True
                continue
            if event.task_id not in self._display_ids:
                task = queue.get(event.task_id)
                if task is not None and self._matches(task):
                    need_reload = True
                continue
            task = queue.get(event.task_id)
            if task is None:
                need_reload = True
                continue
            if task.status != self._status_cache.get(event.task_id):
                need_reload = True
                continue
            self._update_cells(task)
        if need_reload:
            self._rebuild()

    def _update_cells(self, task: Task) -> None:
        table = self.query_one("#q-table", DataTable)
        try:
            table.update_cell(task.id, "progress", self._progress_cell(task, self._content_width(table, "progress")))
            live = task.status == RUNNING
            if self._narrow:
                table.update_cell(
                    task.id, "size", rcell(self._size_text(task), self._content_width(table, "size"))
                )
            else:
                table.update_cell(task.id, "speed", rcell(human_speed(task.speed) if live else "—", self._content_width(table, "speed")))
                table.update_cell(task.id, "eta", rcell(human_eta(task.eta) if live else "—", self._content_width(table, "eta")))
                table.update_cell(
                    task.id, "size", rcell(self._size_text(task), self._content_width(table, "size"))
                )
        except Exception:
            # 行已被筛选掉 / 表格正在重建：下一批事件或 reload 会修复
            return

    @staticmethod
    def _content_width(table: DataTable, key: str) -> int:
        column = table.columns.get(key)
        if column is None or column.width is None:
            return 10
        return max(4, int(column.width))

    def on_resize(self, event) -> None:
        self._rebuild()

    # ------------------------------------------------------------ 交互

    def _current_id(self) -> str | None:
        table = self.query_one("#q-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._display_ids)):
            return None
        return self._display_ids[row]

    def _current(self) -> Task | None:
        task_id = self._current_id()
        return self.app.get_queue().get(task_id) if task_id else None

    @on(Button.Pressed, ".q-filter")
    def _on_filter(self, event: Button.Pressed) -> None:
        key = (event.button.id or "").removeprefix("f-")
        if key:
            self.filter_key = key
            self._rebuild()

    @on(Input.Changed, "#q-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._search = event.value.strip()
        self._rebuild()

    @on(Button.Pressed, "#q-new")
    def _on_new(self) -> None:
        self.action_new_download()

    @on(Button.Pressed, "#q-pause")
    def _on_pause(self) -> None:
        self.action_toggle_pause()

    @on(Button.Pressed, "#q-stop")
    def _on_stop(self) -> None:
        self.action_stop_task()

    @on(Button.Pressed, "#q-retry")
    def _on_retry(self) -> None:
        self.action_retry_task()

    @on(Button.Pressed, "#q-remove")
    def _on_remove(self) -> None:
        self.action_remove_task()

    @on(Button.Pressed, "#q-conc")
    def _on_conc(self) -> None:
        current = int(self.app.get_settings().get("max_concurrent_tasks", 2))

        def validator(value: str) -> bool:
            try:
                number = int(value)
            except ValueError:
                return False
            return 1 <= number <= 8

        self.app.push_screen(
            InputModal(
                f"同时下载的任务数（当前 {current}）：",
                title="并发任务数",
                value=str(current),
                validator=validator,
                error="请输入 1-8 之间的整数",
                hint="立即生效，不影响运行中的任务",
            ),
            self._apply_concurrency,
        )

    def _apply_concurrency(self, value: str | None) -> None:
        if value is None:
            return
        config = dict(self.app.get_settings())
        config["max_concurrent_tasks"] = int(value)
        try:
            save_config(config)
        except Exception as exc:
            self.app.notify_err(f"保存失败：{exc}")
            return
        self.app.reload_config()
        self.app.get_queue().apply_settings()
        self.app.notify_ok(f"并发任务数已调整为 {int(value)}")

    @on(DataTable.RowSelected, "#q-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        task_id = event.row_key.value
        if task_id is not None:
            self._open_detail(str(task_id))

    @on(DataTable.RowHighlighted, "#q-table")
    def _on_row_highlight(self, event: DataTable.RowHighlighted) -> None:
        self._update_controls()

    def _open_detail(self, task_id: str) -> None:
        # 防止单击与键盘事件重复压栈
        if isinstance(self.app.screen, TaskDetailModal):
            return
        task = self.app.get_queue().get(task_id)
        if task is None:
            return
        self.app.push_screen(TaskDetailModal(task.to_dict(), self.app.get_queue()))

    # ------------------------------------------------------------ 动作

    def action_new_download(self) -> None:
        from .new_download import NewDownloadScreen

        self.app.push_screen(NewDownloadScreen())

    def action_focus_search(self) -> None:
        self.query_one("#q-search", Input).focus()

    def action_toggle_pause(self) -> None:
        task = self._current()
        if task is None:
            self.app.notify_warn("请先选择一个任务")
            return
        if not QueueManager.can_pause():
            self.app.notify_warn("当前系统不支持暂停下载")
            return
        queue = self.app.get_queue()
        if task.status == PAUSED:
            if not queue.resume(task.id):
                self.app.notify_warn("继续失败：进程可能已退出")
        elif task.status == RUNNING:
            if not queue.pause(task.id):
                self.app.notify_warn("暂停失败：进程可能已退出")
        else:
            self.app.notify_warn("只有下载中或已暂停的任务可以暂停 / 继续")

    def action_stop_task(self) -> None:
        task = self._current()
        if task is None:
            self.app.notify_warn("请先选择一个任务")
            return
        if task.status not in STOPPABLE:
            self.app.notify_warn("该任务已停止，无需停止")
            return
        self._stop(task.id)

    @work
    async def _stop(self, task_id: str) -> None:
        try:
            await self.app.get_queue().cancel(task_id)
        except Exception as exc:  # 进程控制失败不应拖垮整个 TUI
            self.app.notify_err(f"停止失败：{exc}")
            return
        self.app.notify_ok("已停止任务")

    def action_retry_task(self) -> None:
        task = self._current()
        if task is None:
            self.app.notify_warn("请先选择一个任务")
            return
        if task.status not in RETRYABLE:
            self.app.notify_warn("任务尚未停止，无法重试")
            return
        self._retry(task.id)

    @work
    async def _retry(self, task_id: str) -> None:
        try:
            ok = await self.app.get_queue().retry(task_id)
        except Exception as exc:
            self.app.notify_err(f"重试失败：{exc}")
            return
        if ok:
            self.app.notify_ok("已重新加入队列")
        else:
            self.app.notify_warn("该任务正在运行，无法重试")

    def action_remove_task(self) -> None:
        task = self._current()
        if task is None:
            self.app.notify_warn("请先选择一个任务")
            return
        if task.status not in REMOVABLE:
            self.app.notify_warn("请先停止任务再删除")
            return
        message = f"确定删除任务「{shorten(task.label, 40)}」吗？已下载的文件不会删除。"
        self.app.push_screen(
            ConfirmModal(message, title="删除确认", yes="删除", default_yes=False),
            lambda confirmed: self._remove(task.id) if confirmed else None,
        )

    @work
    async def _remove(self, task_id: str) -> None:
        try:
            await self.app.get_queue().remove(task_id)
        except Exception as exc:
            self.app.notify_err(f"删除失败：{exc}")
            return
        self.app.notify_ok("已删除任务")
        self._rebuild()


__all__ = ["QueueScreen", "FILTERS"]
