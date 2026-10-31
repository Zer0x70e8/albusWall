#
"""可折叠容器的动画策略。

:class:`CollapseAnimation` 是「无动画」的默认实现：直接切换显隐 / 高度。
:class:`HeightSlideAnimation` 用 ``maximumHeight`` 做一段高度渐变。
"""

from PySide6.QtCore import QPropertyAnimation, QEasingCurve

__all__ = ["CollapseAnimation", "HeightSlideAnimation"]

_QWIDGETSIZE_MAX = 16777215


class CollapseAnimation:
    """折叠动画的基类 / 默认实现（无动画）。"""

    def stop(self) -> None:
        """停止当前动画（如有）。"""

    def animate_expand(self, content_widget, group_box,
                       on_finished=None) -> None:
        """展开：解除高度限制、显示内容，然后回调。"""
        content_widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        content_widget.show()
        if on_finished:
            on_finished()

    def animate_collapse(self, content_widget, group_box,
                         on_finished=None) -> None:
        """折叠：隐藏内容并把高度压到 0，然后回调。"""
        content_widget.hide()
        content_widget.setMaximumHeight(0)
        if on_finished:
            on_finished()


class HeightSlideAnimation(CollapseAnimation):
    """基于 ``maximumHeight`` 的滑动动画。"""

    def __init__(self, duration: int = 250):
        super().__init__()
        self._animation: QPropertyAnimation | None = None
        self._duration = duration

    def stop(self) -> None:
        if (self._animation is not None
                and self._animation.state() == QPropertyAnimation.State.Running):
            self._animation.stop()

    def animate_expand(self, content_widget, group_box,
                       on_finished=None) -> None:
        self.stop()

        # 先显示并解除限制，让布局算出完整高度
        content_widget.show()
        content_widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        full_height = content_widget.sizeHint().height()

        # 从当前可见高度（折叠时为 0）开始增长，支持动画被打断
        start_h = 0 if not content_widget.isVisible() else content_widget.height()
        content_widget.setMaximumHeight(start_h)
        self._start_animation(content_widget, start_h, full_height, on_finished)

    def animate_collapse(self, content_widget, group_box,
                         on_finished=None) -> None:
        self.stop()
        start_h = content_widget.height()
        # 锁住当前高度，避免折叠瞬间跳变
        content_widget.setMaximumHeight(start_h)
        self._start_animation(content_widget, start_h, 0, on_finished)

    def _start_animation(self, target, start_value: int, end_value: int,
                         on_finished) -> None:
        self._animation = QPropertyAnimation(target, b"maximumHeight")
        self._animation.setDuration(self._duration)
        self._animation.setStartValue(start_value)
        self._animation.setEndValue(end_value)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

        if on_finished:
            # 只有自然结束才回调；被 stop() 打断时不回调
            self._animation.finished.connect(on_finished)

        self._animation.start()
