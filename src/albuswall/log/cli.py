#
"""CLI 日志配置：观感等同 print，按级别自动分流到 stdout / stderr。"""

from __future__ import annotations

import logging
from logging.config import dictConfig

from .filters import BelowLevel
from .logger import getLogger
from .common import to_levelno

DEFAULT_CLI_LOGGER = "albuswall.cli"


def setup_cli_logging(
        name: str = DEFAULT_CLI_LOGGER,
        *,
        level: int | str = "DEBUG",
        fmt: str = "%(message)s",
        datefmt: str | None = None,
        stream_level: int | str = "WARNING",
) -> logging.Logger:
    """配置 CLI 专用的单 logger + 双 stream handler。

    - ``< stream_level`` 的记录走 **stdout**
    - ``>= stream_level`` 的记录走 **stderr**
    - 二者互斥（由 :class:`BelowLevel` filter 保证）
    - ``fmt`` 默认 ``%(message)s``，输出与 ``print`` 无异
    """
    stream_levelno = to_levelno(stream_level)
    levelno = to_levelno(level)

    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {"format": fmt, "datefmt": datefmt},
        },
        "filters": {
            "below_stream": {
                "()": BelowLevel,
                "level": stream_levelno,
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
                "stream": "ext://sys.stdout",
                "filters": ["below_stream"],
            },
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
                "stream": "ext://sys.stderr",
                "level": stream_levelno,
            },
        },
        "loggers": {
            name: {
                "handlers": ["stdout", "stderr"],
                "level": levelno,
                "propagate": False,
            },
        },
    })
    return getLogger(name)
