"""主菜单屏：艺术字 logo（窄终端自动降级）+ 版本副标题 + 居中菜单 + 底部提示栏。"""

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static

from .. import __version__
from ..ui import LOGO_WIDTH, home_col_width, logo_text
from ..widgets import HintBar, MenuMarquee


class HomeScreen(Screen):
    """功能主菜单：新建下载 / 任务队列 / 下载历史 / 设置 / 退出。"""

    BINDINGS = [
        Binding("escape,ctrl+c", "quit_app", "退出"),
        Binding("1", "menu('new')", "新建下载", show=False),
        Binding("2", "menu('queue')", "任务队列", show=False),
        Binding("3", "menu('history')", "下载历史", show=False),
        Binding("4", "menu('settings')", "设置", show=False),
        Binding("5", "menu('quit')", "退出", show=False),
    ]

    ITEMS = [
        ("new", "新建下载", "向队列中加入一些新的任务"),
        ("queue", "任务队列", "查看和管理现有的任务"),
        ("history", "下载历史", "过去的任务记录"),
        ("settings", "设置", "对程序的行为进行自定义"),
        ("quit", "退出程序", "请确认任务是否完成"),
    ]

    def __init__(self) -> None:
        super().__init__(classes="logo-page")

    def compose(self):
        with Vertical(id="home-main"):
            with Vertical(id="home-col"):
                yield Static(logo_text(), id="home-banner")
                yield Static(Text("YT-DLP TUI", style="bold"), id="home-plain")
                yield Static(f"v{__version__}", id="home-version")
                yield OptionList(id="home-list")
        yield HintBar("↑↓ 选择 · 回车 进入 · 1-5 直达 · Esc/Ctrl+C 退出", classes="page-hint")

    def on_mount(self) -> None:
        ol = self.query_one("#home-list", OptionList)
        self._marquee = MenuMarquee(self, "home-list", [(label, desc) for _, label, desc in self.ITEMS])
        ol.add_options(self._marquee.placeholder_options())
        ol.highlighted = 0
        ol.focus()
        self._apply_breakpoint()
        self.call_after_refresh(self._marquee.start)

    def on_resize(self, event) -> None:
        self._apply_breakpoint()
        marquee = getattr(self, "_marquee", None)
        if marquee is not None:
            marquee.sync()

    def _apply_breakpoint(self) -> None:
        """宽度不足以容纳艺术字时降级为普通标题文本；列宽跟随 logo。"""
        small = self.size.width < LOGO_WIDTH + 6
        self.set_class(small, "-sm")
        self.query_one("#home-col").styles.width = None if small else home_col_width()

    def _open(self, key: str) -> None:
        if key == "quit":
            self.app.action_quit()
            return

        from .new_download import NewDownloadScreen
        from .queue import QueueScreen
        from .settings import SettingsScreen

        if key == "new":
            self.app.push_screen(NewDownloadScreen())
        elif key == "queue":
            self.app.push_screen(QueueScreen())
        elif key == "history":
            self.app.push_screen(QueueScreen(initial_filter="done"))
        elif key == "settings":
            self.app.push_screen(SettingsScreen())

    @on(OptionList.OptionSelected, "#home-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        self._open(self.ITEMS[event.option_index][0])

    def action_menu(self, key: str) -> None:
        self._open(key)

    def action_quit_app(self) -> None:
        self.app.action_quit()
