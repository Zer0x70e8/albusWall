#
""""""

from typing import Optional, Callable

from PySide6.QtCore import (
    QObject, QPropertyAnimation, QEasingCurve, QRect, QAbstractAnimation,
)
from PySide6.QtWidgets import QWidget


class GeometryAnimator(QObject):
    """基于 QPropertyAnimation 的动画策略，符合 SearchAnimator 协议。"""

    def __init__(self, target: QWidget, duration: int = 250, parent=None):
        super().__init__(parent)
        self._target = target

        self._anim = QPropertyAnimation(target, b"geometry", self)
        self._anim.setDuration(duration)
        # ✅ 显式枚举作用域，IDE 才能解析
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
        """取出并清空回调，只调用一次。显式检查 None，IDE 才不报警。"""
        cb = self._on_finished       # 先读到局部变量
        self._on_finished = None     # 再清空（防止重入）
        if cb is not None:
            # noinspection calling-non-callable
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
