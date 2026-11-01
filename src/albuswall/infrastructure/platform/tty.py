# -*- coding: utf-8 -*-
"""交互终端判定。"""

from __future__ import annotations

import sys


def is_interactive() -> bool:
    """当前进程是否挂在一个交互式终端上。

    双击启动 GUI 时 stdin 通常不是 tty，会返回 False——这正是期望行为：
    入口据此选择「非交互 + 有桌面 → 启动 GUI」。
    """
    try:
        if sys.stdin is None:
            return False
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False
