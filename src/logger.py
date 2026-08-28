"""日志：控制台 + 滚动文件（data/logs/autopub.log）。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .constants import LOG_DIR

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志器（幂等，可重复调用）。"""
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(level)
        return

    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / "autopub.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FMT))
        root.addHandler(file_handler)
    except OSError:
        root.warning("日志文件初始化失败，仅输出到控制台")

    # 三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
