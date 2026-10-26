"""设置屏：三个 kv 面板（下载 / 网络与认证 / 后处理与环境），右下角按钮弹表单编辑。"""

from __future__ import annotations

import asyncio
import re

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from ..config import BROWSER_KEYS, PRESET_KEYS, PRESETS, detect_env_proxy, save_config
from ..ui import PageScreen
from ..widgets import STYLE_DIM, STYLE_OK, STYLE_WARN, FormField, FormModal, OutputModal, busy
from ..ytdlp import YtdlpError, check_update, ffmpeg_binary, ffmpeg_version, version_of, ytdlp_binary

PANELS = {
    "download": "下载",
    "network": "网络与认证",
    "post": "后处理与环境",
}

BROWSER_LABELS = {"": "不使用", "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "brave": "Brave"}


def _mask_proxy(url: str) -> str:
    """代理 URL 密码脱敏：scheme://user:pass@host → scheme://user:***@host。"""
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", url)


def _int_validator(low: int, high: int, label: str):
    def check(value: str) -> bool:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return False
        return low <= number <= high

    check.__doc__ = f"{label} 必须是 {low}-{high} 的整数"
    return check


class SettingsScreen(PageScreen):
    """全局设置：读写 config.yaml，保存后立即生效（含并发数热更新）。"""

    TITLE = "设置"
    HINT = "点击「编辑」修改 · PgUp/PgDn 滚动 · Ctrl+R 刷新 · Esc/Ctrl+C 返回"

    DEFAULT_CSS = """
    SettingsScreen #settings-scroll { height: 1fr; }
    SettingsScreen .set-title { height: 1; text-style: bold; color: $primary; }
    SettingsScreen .kv-value.wrap { text-wrap: nowrap; text-overflow: ellipsis; overflow: hidden; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}

    # ------------------------------------------------------------ 组合

    def compose_page(self):
        with VerticalScroll(id="settings-scroll"):
            with Vertical(classes="panel tight"):
                yield Static("下载", classes="set-title")
                yield from self._kv("下载目录", "download_dir")
                yield from self._kv("文件名模板", "filename_template")
                yield from self._kv("默认预设", "default_preset")
                yield from self._kv("容器", "merge_container")
                yield from self._kv("并发任务", "max_concurrent_tasks")
                yield from self._kv("片段并发", "concurrent_fragments")
                yield from self._kv("限速", "rate_limit")
                yield from self._kv("加入后跳转队列", "open_queue_after_add")
                with Horizontal(classes="filter-row gap-top"):
                    yield Static(classes="fill")
                    yield Button("编辑", id="edit-download", variant="primary")

            with Vertical(classes="panel tight"):
                yield Static("网络与认证", classes="set-title")
                yield from self._kv("自动检测代理", "auto_proxy")
                yield from self._kv("代理", "proxy")
                yield from self._kv("使用系统证书库", "use_system_certs")
                yield from self._kv("跳过证书校验", "no_check_certificates")
                yield from self._kv("cookies 文件", "cookies_file")
                yield from self._kv("浏览器 cookies", "cookies_from_browser")
                yield from self._kv("重试次数", "retries")
                yield from self._kv("历史保留", "history_limit")
                with Horizontal(classes="filter-row gap-top"):
                    yield Static(classes="fill")
                    yield Button("编辑", id="edit-network", variant="primary")

            with Vertical(classes="panel tight"):
                yield Static("后处理与环境", classes="set-title")
                yield from self._kv("字幕语言", "sub_langs")
                yield from self._kv("嵌入字幕", "embed_subs")
                yield from self._kv("嵌入元数据", "embed_metadata")
                yield from self._kv("嵌入封面", "embed_thumbnail")
                yield from self._kv("SponsorBlock", "sponsorblock_remove")
                yield from self._kv("yt-dlp 路径", "ytdlp_path")
                yield from self._kv("ffmpeg 路径", "ffmpeg_path")
                yield from self._kv("忽略用户配置", "ignore_ytdlp_config")
                with Horizontal(classes="kv-row gap-top"):
                    yield Static("版本", classes="kv-label")
                    yield Static("检测中…", classes="kv-value wrap", id="st-versions")
                with Horizontal(classes="filter-row gap-top"):
                    yield Static(classes="fill")
                    yield Button("检查 yt-dlp 更新", id="st-update")

    def _kv(self, label: str, key: str):
        with Horizontal(classes="kv-row"):
            yield Static(label, classes="kv-label")
            yield Static("", classes="kv-value wrap", id=f"view-{key}")

    def on_mount(self) -> None:
        self._config = dict(self.app.get_settings())
        self._refresh()
        self._probe_versions()
        self.query_one("#edit-download", Button).focus()

    def reload_page(self) -> None:
        self._config = dict(self.app.get_settings())
        self._refresh()
        self._probe_versions()

    # ------------------------------------------------------------ 展示

    def _refresh(self) -> None:
        for key in (
            "download_dir",
            "filename_template",
            "default_preset",
            "merge_container",
            "max_concurrent_tasks",
            "concurrent_fragments",
            "rate_limit",
            "proxy",
            "cookies_file",
            "cookies_from_browser",
            "retries",
            "history_limit",
            "sub_langs",
            "sponsorblock_remove",
            "ytdlp_path",
            "ffmpeg_path",
        ):
            try:
                self.query_one(f"#view-{key}", Static).update(self._display_value(key))
            except Exception:
                continue
        for key in (
            "auto_proxy",
            "use_system_certs",
            "no_check_certificates",
            "open_queue_after_add",
            "embed_subs",
            "embed_metadata",
            "embed_thumbnail",
            "ignore_ytdlp_config",
        ):
            try:
                value = bool(self._config.get(key))
                # 「跳过证书校验」属降级操作，开启时用警示色而非成功色
                style = (STYLE_WARN if key == "no_check_certificates" else STYLE_OK) if value else STYLE_DIM
                self.query_one(f"#view-{key}", Static).update(Text("开启" if value else "关闭", style=style))
            except Exception:
                continue

    def _display_value(self, key: str) -> Text:
        value = self._config.get(key, "")
        if key == "proxy":
            return self._display_proxy()
        if key == "default_preset":
            return Text(PRESETS.get(str(value), PRESETS["best"])["label"])
        if key == "merge_container":
            return Text({"auto": "自动（按源）", "mp4": "MP4", "mkv": "MKV"}.get(str(value), str(value)))
        if key == "cookies_from_browser":
            return Text(BROWSER_LABELS.get(str(value), str(value) or "不使用"))
        if key in ("cookies_file", "ytdlp_path", "ffmpeg_path", "rate_limit", "sponsorblock_remove", "sub_langs"):
            return Text(str(value) or "未设置", style="" if value else STYLE_DIM)
        return Text(str(value))

    def _display_proxy(self) -> Text:
        """代理展示：自动模式下显示环境变量探测结果（该值不会写入配置文件）。"""
        if self._config.get("auto_proxy", True):
            proxy, source = detect_env_proxy()
            if proxy:
                return Text(f"自动 · {_mask_proxy(proxy)}（{source}）", style=STYLE_OK)
            return Text("自动 · 未检测到环境代理", style=STYLE_DIM)
        proxy = str(self._config.get("proxy", "")).strip()
        if proxy:
            return Text(_mask_proxy(proxy))
        return Text('直连（--proxy ""）', style=STYLE_DIM)

    @work(exclusive=True)
    async def _probe_versions(self) -> None:
        settings = self.app.get_settings()
        ytdlp_text = "未找到"
        ffmpeg_text = "未找到"
        try:
            binary = ytdlp_binary(settings)
            ytdlp_text = await asyncio.to_thread(version_of, binary)
        except YtdlpError:
            ytdlp_text = "未找到"
        except Exception:
            ytdlp_text = "检测失败"
        try:
            ffmpeg = ffmpeg_binary(settings)
            ffmpeg_text = await asyncio.to_thread(ffmpeg_version, ffmpeg) if ffmpeg else "未找到"
        except Exception:
            ffmpeg_text = "检测失败"
        try:
            text = Text(f"yt-dlp {ytdlp_text} · ffmpeg {ffmpeg_text or '未找到'}")
            if "未找到" in f"{ytdlp_text}{ffmpeg_text}":
                text.stylize(STYLE_WARN)
            self.query_one("#st-versions", Static).update(text)
        except Exception:
            return

    # ------------------------------------------------------------ 编辑

    @on(Button.Pressed, "#edit-download")
    def _edit_download(self) -> None:
        fields = [
            FormField("download_dir", "下载目录", value=str(self._config.get("download_dir", ""))),
            FormField("filename_template", "文件名模板", value=str(self._config.get("filename_template", ""))),
            FormField(
                "default_preset",
                "默认预设",
                kind="choice",
                value=str(self._config.get("default_preset", "best")),
                # 「自定义格式 ID」必须逐任务选择格式，不能作为默认预设
                options=[(PRESETS[k]["label"], k) for k in PRESET_KEYS if k != "custom"],
            ),
            FormField(
                "merge_container",
                "容器",
                kind="choice",
                value=str(self._config.get("merge_container", "auto")),
                options=[("自动（按源）", "auto"), ("MP4", "mp4"), ("MKV", "mkv")],
            ),
            FormField(
                "max_concurrent_tasks",
                "并发任务",
                value=str(self._config.get("max_concurrent_tasks", 2)),
                validator=_int_validator(1, 8, "并发任务"),
                error="必须是 1-8 的整数",
            ),
            FormField(
                "concurrent_fragments",
                "片段并发",
                value=str(self._config.get("concurrent_fragments", 4)),
                validator=_int_validator(0, 16, "片段并发"),
                error="必须是 0-16 的整数（0=不指定）",
            ),
            FormField("rate_limit", "限速", value=str(self._config.get("rate_limit", "")), placeholder="如 5M，空=不限"),
            FormField(
                "open_queue_after_add",
                "加入后跳转队列",
                kind="switch",
                value="on" if self._config.get("open_queue_after_add") else "",
            ),
        ]
        self.app.push_screen(FormModal("编辑下载设置", fields), lambda data: self._save("download", data))

    @on(Button.Pressed, "#edit-network")
    def _edit_network(self) -> None:
        browser_options = [(BROWSER_LABELS.get(key, key or "不使用"), key) for key in BROWSER_KEYS]
        env_proxy, env_source = detect_env_proxy()
        proxy_placeholder = f"环境代理：{_mask_proxy(env_proxy)}（{env_source}）" if env_proxy else "未检测到环境代理"
        fields = [
            FormField(
                "auto_proxy",
                "自动检测代理",
                kind="switch",
                value="on" if self._config.get("auto_proxy", True) else "",
                hint="由 yt-dlp 读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY",
            ),
            FormField(
                "proxy",
                "代理",
                value=str(self._config.get("proxy", "")),
                placeholder=proxy_placeholder,
                disabled_by="auto_proxy",
                hint='留空 = 直连；关闭自动检测后生效',
            ),
            FormField(
                "use_system_certs",
                "使用系统证书库",
                kind="switch",
                value="on" if self._config.get("use_system_certs") else "",
                hint="改用系统证书库，支持自签名根证书",
            ),
            FormField(
                "no_check_certificates",
                "跳过证书校验",
                kind="switch",
                value="on" if self._config.get("no_check_certificates") else "",
                hint="不校验 HTTPS 证书，仅用于自签名或中间人代理",
            ),
            FormField(
                "cookies_file",
                "cookies 文件",
                value=str(self._config.get("cookies_file", "")),
                placeholder="/path/to/cookies.txt",
            ),
            FormField(
                "cookies_from_browser",
                "浏览器 cookies",
                kind="choice",
                value=str(self._config.get("cookies_from_browser", "")) or "",
                options=browser_options,
            ),
            FormField(
                "retries",
                "重试次数",
                value=str(self._config.get("retries", 10)),
                validator=_int_validator(0, 100, "重试次数"),
                error="必须是 0-100 的整数",
            ),
            FormField(
                "history_limit",
                "历史保留",
                value=str(self._config.get("history_limit", 200)),
                validator=_int_validator(20, 5000, "历史保留"),
                error="必须是 20-5000 的整数",
            ),
        ]
        self.app.push_screen(FormModal("编辑网络与认证", fields), lambda data: self._save("network", data))

    @on(Button.Pressed, "#edit-post")
    def _edit_post(self) -> None:
        fields = [
            FormField("sub_langs", "字幕语言", value=str(self._config.get("sub_langs", "")), placeholder="如 zh.*,en.*"),
            FormField("embed_subs", "嵌入字幕", kind="switch", value="on" if self._config.get("embed_subs") else ""),
            FormField(
                "embed_metadata",
                "嵌入元数据",
                kind="switch",
                value="on" if self._config.get("embed_metadata") else "",
            ),
            FormField(
                "embed_thumbnail",
                "嵌入封面",
                kind="switch",
                value="on" if self._config.get("embed_thumbnail") else "",
            ),
            FormField(
                "sponsorblock_remove",
                "SponsorBlock",
                value=str(self._config.get("sponsorblock_remove", "")),
                placeholder="如 sponsor，空=关闭",
            ),
            FormField("ytdlp_path", "yt-dlp 路径", value=str(self._config.get("ytdlp_path", "")), placeholder="留空自动探测"),
            FormField("ffmpeg_path", "ffmpeg 路径", value=str(self._config.get("ffmpeg_path", "")), placeholder="留空自动探测"),
            FormField(
                "ignore_ytdlp_config",
                "忽略用户配置",
                kind="switch",
                value="on" if self._config.get("ignore_ytdlp_config") else "",
                hint="开启后加 --ignore-config，避免 ~/.config/yt-dlp/config 干扰",
            ),
        ]
        self.app.push_screen(FormModal("编辑后处理与环境", fields), lambda data: self._save("post", data))

    def _save(self, panel: str, data: dict | None) -> None:
        if not data:
            return
        new_config = dict(self.app.get_settings())
        for key, value in data.items():
            if key in ("max_concurrent_tasks", "concurrent_fragments", "retries", "history_limit"):
                try:
                    new_config[key] = int(str(value))
                except (TypeError, ValueError):
                    self.app.notify_err(f"{key} 必须是整数")
                    return
            else:
                new_config[key] = value
        try:
            save_config(new_config)
        except Exception as exc:
            self.app.notify_err(f"保存失败：{exc}")
            return
        warnings = self.app.reload_config()
        self._config = dict(self.app.get_settings())
        self.app.get_queue().apply_settings()
        self._refresh()
        for warning in warnings:
            self.app.notify_warn(warning)
        self.app.notify_ok(f"已保存{PANELS.get(panel, '')}设置")

    # ------------------------------------------------------------ 更新

    @on(Button.Pressed, "#st-update")
    def _on_update(self) -> None:
        self._check_update()

    @work(exclusive=True)
    async def _check_update(self) -> None:
        output = ""
        ok = False
        try:
            async with busy(self.app, "正在执行 yt-dlp -U…", mode="bar"):
                ok, output = await asyncio.to_thread(check_update, self.app.get_settings())
        except YtdlpError as exc:
            self.app.notify_err(str(exc))
            return
        except Exception as exc:
            self.app.notify_err(f"检查更新失败：{exc}")
            return
        self.app.push_screen(OutputModal("yt-dlp 更新结果", output))
        if ok:
            self.app.notify_ok("已执行更新检查")
        else:
            self.app.notify_warn("更新失败，请查看输出")
        self._probe_versions()


__all__ = ["SettingsScreen"]
