#
"""搜索栏展开 / 折叠动画的策略协议。

控件只依赖这个 :class:`typing.Protocol`；具体动画实现（``GeometryAnimator``）
或纯静态实现（传 ``None``）都可以塞进去，互不耦合。
"""

from typing import Callable, Protocol

from PySide6.QtCore import QRect

__all__ = ["SearchAnimator"]


class SearchAnimator(Protocol):
    def start_expand(self, start: QRect, end: QRect,
                     on_finished: Callable[[], None]) -> None: ...

    def start_collapse(self, start: QRect, end: QRect,
                       on_finished: Callable[[], None]) -> None: ...

    def update_target(self, rect: QRect) -> None: ...

    def cancel(self) -> None: ...
