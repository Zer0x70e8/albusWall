#
"""``albuswall.ui.keymap`` 的异常类型。

全部继承 :class:`KeymapError`，调用方可以「一条 except 兜住整层」；需要细分
的地方再按子类判断。注意 :class:`InvalidKeySequence` 同时是 ``ValueError``——
键位字符串非法本质上就是「值不对」，保持与 ``ValueError`` 系调用方兼容。
"""

from __future__ import annotations

__all__ = [
    "KeymapError",
    "InvalidKeySequence",
    "DuplicateShortcutSlot",
    "ShortcutConfigError",
]


class KeymapError(Exception):
    """快捷键层（注册 / 绑定 / 配置）的错误基类。"""


class InvalidKeySequence(KeymapError, ValueError):
    """键位字符串无法解析成合法的 Qt 快捷键序列。"""


class DuplicateShortcutSlot(KeymapError):
    """同名快捷键槽被重复注册（且未显式 ``replace=True``）。"""


class ShortcutConfigError(KeymapError):
    """快捷键配置文件读取 / 解析 / 写回失败。"""
