#
"""``albuswall.ui.nav`` 的异常。

导航层只做「注册 + 决策」，因此异常只描述**声明错误**（重复注册、引用不存在
的域 / 节点、网格坐标非法），不描述运行时边界——边界不是错误，动作返回
``None`` 即可。
"""

from __future__ import annotations

__all__ = [
    "DuplicateNavDomain",
    "DuplicateNavNode",
    "InvalidNavGrid",
    "NavError",
    "NavGridCollision",
    "UnknownNavDomain",
    "UnknownNavNode",
    "UnknownNavPage",
]


class NavError(Exception):
    """导航层基类异常。"""


class DuplicateNavDomain(NavError):
    """域名已注册。"""


class DuplicateNavNode(NavError):
    """节点 id 已注册。"""


class UnknownNavDomain(NavError):
    """引用了未注册的域。"""


class UnknownNavNode(NavError):
    """引用了未注册的节点。"""


class UnknownNavPage(NavError):
    """页面未通过 ``ensure_page`` 创建。"""


class InvalidNavGrid(NavError):
    """``GridPlacement`` 非法（负值或 span < 1）。"""


class NavGridCollision(NavError):
    """网格碰撞。v1 不抛出；预留给 v2。"""
