#
"""基于 ``QPropertyAnimation`` 的几何动画，符合 :class:`SearchAnimator` 协议。"""

from typing import Callable, Optional

from PySide6.QtCore import (
    QObject, QPropertyAnimation, QEasingCurve, QRect, QAbstractAnimation,
)
from PySide6.QtWidgets import QWidget

__all__ = ["GeometryAnimator"]


class GeometryAnimator(QObject):
    """把控件 geometry 从 start 平滑过渡到 end 的动画策略。"""

    def __init__(self, target: QWidget, duration: int = 250, parent=None):
        super().__init__(parent)
        self._target = target

        self._anim = QPropertyAnimation(target, b"geometry", self)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._on_finished: Optional[Callable[[], None]] = None
        self._anim.finished.connect(self._handle_finished)

    # --------------------------------------------------------------- 内部
    def _start(self,
               start: QRect,
               end: QRect,
               on_finished: Callable[[], None]) -> None:
        # 打断旧动画（且不触发旧回调）
        if self._anim.state() == QAbstractAnimation.State.Running:
            self._anim.stop()

        self._on_finished = on_finished

        if start == end:
            self._target.setGeometry(end)
            self._invoke_finished()
            return

        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _invoke_finished(self) -> None:
        """取出并清空回调，只调用一次（防重入）。"""
        cb = self._on_finished
        self._on_finished = None
        if cb is not None:
            cb()

    def _handle_finished(self) -> None:
        self._invoke_finished()

    # ------------------------------------------------- SearchAnimator 协议
    def start_expand(self,
                     start: QRect,
                     end: QRect,
                     on_finished: Callable[[], None]) -> None:
        self._start(start, end, on_finished)

    def start_collapse(self,
                       start: QRect,
                       end: QRect,
                       on_finished: Callable[[], None]) -> None:
        self._start(start, end, on_finished)

    def update_target(self, rect: QRect) -> None:
        if self._anim.state() == QAbstractAnimation.State.Running:
            self._anim.setEndValue(rect)

    def cancel(self) -> None:
        if self._anim.state() == QAbstractAnimation.State.Running:
            self._anim.stop()
        self._on_finished = None
