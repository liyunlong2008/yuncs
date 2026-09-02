"""loguru 日志配置：控制台 + 滚动文件。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    Path("logs").mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, enqueue=True)
    logger.add(
        "logs/bot_{time:YYYYMMDD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="14 days",
        enqueue=True,
        encoding="utf-8",
    )
