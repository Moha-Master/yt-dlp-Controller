"""命令行入口：解析参数、降噪日志并启动 Textual TUI。

``-D/--dir`` 必须在 import .app 之前写入环境变量，保证配置层启动即读到正确目录。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR, YTDLP_PATH_ENV


def _setup_logging() -> None:
    """Textual 运行期禁止日志污染终端；只保留 CRITICAL。"""
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in ("asyncio", "urllib3", "requests", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="yt-dlp 图形化终端界面（Textual TUI）")
    parser.add_argument(
        "-D",
        "--dir",
        help=f"配置目录（默认 {DEFAULT_CONFIG_DIR}）",
        default=str(Path(DEFAULT_CONFIG_DIR).expanduser()),
    )
    parser.add_argument(
        "--yt-dlp",
        dest="ytdlp_path",
        default="",
        help="yt-dlp 可执行文件路径（覆盖配置项）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"yt-dlp-ctl {__version__}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = parse_args(argv)
    os.environ[CONFIG_DIR_ENV] = str(Path(args.dir).expanduser())
    if args.ytdlp_path:
        os.environ[YTDLP_PATH_ENV] = args.ytdlp_path

    from .app import YtdlpTuiApp

    YtdlpTuiApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
