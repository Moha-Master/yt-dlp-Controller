"""配置层：目录解析、默认值、校验回落、读写。

纯数据模块：零 Textual 依赖、禁止 print。非法配置一律回落到默认值并返回告警，
不阻断启动（区别于需要强制向导的项目）。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR_ENV = "YT_DLP_CONTROLLER_CONFIG_DIR"
YTDLP_PATH_ENV = "YT_DLP_CONTROLLER_YTDLP"
DEFAULT_CONFIG_DIR = "~/.config/yt-dlp-controller"

# 格式预设（供界面下拉与 yt-dlp 参数构建共用）
PRESETS: dict[str, dict[str, str]] = {
    "best": {"label": "最佳画质（视频+音频）", "format": "bv*+ba/b", "audio_format": ""},
    "2160": {"label": "≤ 4K", "format": "bv*[height<=2160]+ba/b[height<=2160]", "audio_format": ""},
    "1440": {"label": "≤ 2K", "format": "bv*[height<=1440]+ba/b[height<=1440]", "audio_format": ""},
    "1080": {"label": "≤ 1080p", "format": "bv*[height<=1080]+ba/b[height<=1080]", "audio_format": ""},
    "720": {"label": "≤ 720p", "format": "bv*[height<=720]+ba/b[height<=720]", "audio_format": ""},
    "480": {"label": "≤ 480p", "format": "bv*[height<=480]+ba/b[height<=480]", "audio_format": ""},
    "audio_best": {"label": "仅音频（最佳）", "format": "ba/b", "audio_format": ""},
    "audio_mp3": {"label": "仅音频（MP3）", "format": "ba/b", "audio_format": "mp3"},
    "audio_m4a": {"label": "仅音频（M4A）", "format": "ba/b", "audio_format": "m4a"},
    "custom": {"label": "自定义格式 ID", "format": "", "audio_format": ""},
}

PRESET_KEYS: tuple[str, ...] = tuple(PRESETS)
AUDIO_PRESETS: tuple[str, ...] = ("audio_best", "audio_mp3", "audio_m4a")
CONTAINER_KEYS: tuple[str, ...] = ("auto", "mp4", "mkv")
BROWSER_KEYS: tuple[str, ...] = ("", "chrome", "chromium", "firefox", "edge", "brave", "safari", "opera", "vivaldi")

DEFAULTS: dict[str, Any] = {
    "download_dir": "~/Downloads/yt-dlp",
    "filename_template": "%(title)s [%(id)s].%(ext)s",
    "default_preset": "best",
    "merge_container": "auto",
    "max_concurrent_tasks": 2,
    "concurrent_fragments": 4,
    "rate_limit": "",
    "retries": 10,
    "cookies_file": "",
    "cookies_from_browser": "",
    # auto_proxy=True：不传 --proxy，由 yt-dlp 自动读取 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY；
    # auto_proxy=False：总是传 --proxy，proxy 为空即 --proxy "" 强制直连。
    "auto_proxy": True,
    "proxy": "",
    "use_system_certs": False,
    "no_check_certificates": False,
    "sub_langs": "zh.*,en.*",
    "embed_subs": False,
    "embed_metadata": True,
    "embed_thumbnail": False,
    "sponsorblock_remove": "",
    "ytdlp_path": "",
    "ffmpeg_path": "",
    "ignore_ytdlp_config": True,
    "history_limit": 200,
    "open_queue_after_add": True,
}

_BOOL_KEYS = {
    "auto_proxy",
    "use_system_certs",
    "no_check_certificates",
    "embed_subs",
    "embed_metadata",
    "embed_thumbnail",
    "ignore_ytdlp_config",
    "open_queue_after_add",
}
_INT_RANGES: dict[str, tuple[int, int]] = {
    "max_concurrent_tasks": (1, 8),
    "concurrent_fragments": (0, 16),
    "retries": (0, 100),
    "history_limit": (20, 5000),
}
_TEXT_KEYS = {"download_dir", "filename_template", "rate_limit", "cookies_file", "proxy", "sub_langs", "sponsorblock_remove", "ytdlp_path", "ffmpeg_path"}
_CHOICE_KEYS: dict[str, tuple[str, ...]] = {
    # 「custom」只能逐任务使用（需配合具体 format_id），不能作为全局默认
    "default_preset": tuple(k for k in PRESET_KEYS if k != "custom"),
    "merge_container": CONTAINER_KEYS,
    "cookies_from_browser": BROWSER_KEYS,
}


def get_config_dir() -> Path:
    """配置目录：环境变量优先，其次默认位置。"""
    return Path(os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)).expanduser()


# 与 urllib.request.getproxies() 的键一致；按 specificity 从高到低排列
ENV_PROXY_KEYS: tuple[str, ...] = (
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def detect_env_proxy() -> tuple[str, str]:
    """检测系统代理：返回 (代理 URL, 来源变量名)；未设置返回 ("", "")。"""
    for key in ENV_PROXY_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value, key
    return "", ""


def detect_binary_path(name: str) -> str:
    """在 PATH 中查找可执行文件；找不到返回空串。"""
    return shutil.which(name) or ""


def _autofill_paths(config: dict[str, Any]) -> bool:
    """yt-dlp / ffmpeg 路径留空时填入 PATH 探测结果；返回是否有改动。"""
    changed = False
    for key, name in (("ytdlp_path", "yt-dlp"), ("ffmpeg_path", "ffmpeg")):
        if str(config.get(key) or "").strip():
            continue
        found = detect_binary_path(name)
        if found:
            config[key] = found
            changed = True
    return changed


def get_config_path() -> Path:
    return get_config_dir() / "config.yaml"


def get_queue_path() -> Path:
    return get_config_dir() / "queue.json"


def config_exists() -> bool:
    return get_config_path().exists()


def default_config() -> dict[str, Any]:
    return dict(DEFAULTS)


def _normalize_value(key: str, raw: Any, warnings: list[str]) -> Any:
    """单字段校验；不合法时记录中文告警并回落默认值。"""
    default = DEFAULTS[key]
    if key in _BOOL_KEYS:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "yes", "on", "1"}:
            return True
        if isinstance(raw, str) and raw.strip().lower() in {"false", "no", "off", "0"}:
            return False
        warnings.append(f"配置项 {key} 非法（{raw!r}），已回落到 {default}")
        return default

    if key in _INT_RANGES:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            warnings.append(f"配置项 {key} 非法（{raw!r}），已回落到 {default}")
            return default
        low, high = _INT_RANGES[key]
        if not low <= value <= high:
            warnings.append(f"配置项 {key} 超出范围 {low}-{high}（{value}），已回落到 {default}")
            return default
        return value

    if key in _CHOICE_KEYS:
        value = str(raw or "").strip()
        if value not in _CHOICE_KEYS[key]:
            warnings.append(f"配置项 {key} 非法（{raw!r}），已回落到 {default}")
            return default
        return value

    if key in _TEXT_KEYS:
        if raw is None:
            return default
        if not isinstance(raw, (str, int, float)):
            warnings.append(f"配置项 {key} 非法（{raw!r}），已回落到 {default}")
            return default
        text = str(raw).strip()
        if key == "download_dir":
            return str(Path(text or default).expanduser())
        return text

    return raw


def normalize_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把任意映射归一为完整配置，返回 (配置, 告警列表)。"""
    warnings: list[str] = []
    source = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        warnings.append("配置文件顶层不是映射，已全部使用默认值")
    config: dict[str, Any] = {}
    for key in DEFAULTS:
        config[key] = _normalize_value(key, source.get(key, DEFAULTS[key]), warnings)
    return config, warnings


def load_config() -> tuple[dict[str, Any], list[str]]:
    """读取配置。文件缺失时生成默认配置；坏值回落并返回告警。

    yt-dlp / ffmpeg 路径留空时自动填入 PATH 探测结果并落盘，方便用户查看与修改；
    环境变量 ``YT_DLP_CONTROLLER_YTDLP`` 仍会覆盖最终运行值。
    """
    path = get_config_path()
    if not path.exists():
        config = default_config()
        config["download_dir"] = str(Path(config["download_dir"]).expanduser())
        _autofill_paths(config)
        save_config(config)
        config, warnings = normalize_config(config)
    else:
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            warnings = [f"配置文件无法读取（{exc}），已使用默认值"]
            config = default_config()
            _autofill_paths(config)
        else:
            config, warnings = normalize_config(raw)
            if _autofill_paths(config):
                try:
                    save_config(config)
                except OSError as exc:
                    warnings.append(f"自动填入可执行文件路径失败（{exc}）")

    override = os.environ.get(YTDLP_PATH_ENV, "").strip()
    if override:
        config["ytdlp_path"] = override

    # 下载目录必须存在才能直接 -P；失败只告警，不阻断启动
    try:
        Path(config["download_dir"]).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warnings.append(f"下载目录不可用（{exc}），请在设置中修改")
    return config, warnings


def save_config(data: dict[str, Any]) -> Path:
    """校验后写入配置（原子替换）。"""
    config, _ = normalize_config(data)
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)
    return path
