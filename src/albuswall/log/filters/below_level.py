#
"""上界 filter：与 Handler.setLevel 的低界互补，实现按级别互斥分流。"""

from __future__ import annotations

import logging

from ..common import to_levelno


class BelowLevel(logging.Filter):
    """只放行 ``levelno < level`` 的记录。

    ``Handler.setLevel(level)`` 表达的是"我接 >= level 的"；单靠它
    无法表达"我只接 < level 的"。把它和本 filter 组合，就能让
    同一个 logger 的两个 handler 严格互斥，例如：

    - stdout handler：``level=NOTSET`` + ``BelowLevel("WARNING")``
    - stderr handler：``level=WARNING``

    """

    def __init__(self, level: int | str) -> None:
        super().__init__()
        self.levelno = to_levelno(level)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.levelno

    def __repr__(self) -> str:
        return f"<{type(self).__name__} levelno={self.levelno}>"
