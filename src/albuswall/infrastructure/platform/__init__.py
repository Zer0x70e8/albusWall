# -*- coding: utf-8 -*-
"""平台相关探测与提示。"""

from .display import has_desktop
from .tty import is_interactive
from .notify import notify_already_running

__all__ = ["has_desktop", "is_interactive", "notify_already_running"]
