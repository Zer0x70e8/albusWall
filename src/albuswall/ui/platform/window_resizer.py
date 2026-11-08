#
"""自绘边框窗口的交互管理器（拖拽移动 / 边缘缩放）。

:class:`WindowResizer` 是「无系统标题栏」（``FramelessWindowHint``）窗口的
交互补全：Qt 拿掉了原生标题栏与原生边框，拖拽移动、边缘缩放这两件事就
必须自己实现。

工作方式：

* 在 ``QApplication`` 上装一个事件过滤器，只关心**属于本窗口**的
  ``MouseMove`` / ``MouseButtonPress`` / ``MouseButtonRelease``；
* 鼠标停在窗口边缘 ``margin`` 像素内 → 换对应的缩放光标；
* 在边缘按下左键 → 交给 ``windowHandle().startSystemResize(edges)``；
* 在标题栏区域按下左键 → 交给 ``windowHandle().startSystemMove()``；
* 特例：窗口最大化时在标题栏按下左键，先 ``showNormal()``，把窗口按
  鼠标在标题栏中的横向比例挪到光标下（并让标题栏贴住光标），再开始拖拽
  —— 也就是各家桌面环境「从最大化拖出来」的手感。

窗口内的交互控件（按钮 / 滑杆 / 下拉框 / 输入框）会拿到自己的点击，不被
拖拽吞掉：这类控件的左键按下不会触发 ``startSystemMove``。
"""

from __future__ import annotations

from enum import IntFlag
from typing import Optional, Union

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QLineEdit,
    QSlider,
    QWidget,
)

__all__ = ["Direction", "WindowResizer"]

#: 落在这些控件上的左键按下不当作「拖拽窗口」，交给控件自己处理。
_INTERACTIVE_TYPES = (QAbstractButton, QSlider, QComboBox, QLineEdit)


class Direction(IntFlag):
    """缩放方向位标志，可用 ``|`` 组合。"""

    NONE = 0
    UP = 1  # 0001
    DOWN = 2  # 0010
    LEFT = 4  # 0100
    RIGHT = 8  # 1000
    TOP_LEFT = UP | LEFT
    TOP_RIGHT = UP | RIGHT
    BOTTOM_LEFT = DOWN | LEFT
    BOTTOM_RIGHT = DOWN | RIGHT

    @staticmethod
    def from_mouse_pos(x: int, y: int,
                       w: int, h: int,
                       margin: int) -> "Direction":
        """按窗口内坐标判定缩放方向。

        * ``x`` 落在左边距内 → 加 ``LEFT``；
        * ``x`` 落在右边距内 → 加 ``RIGHT``；
        * ``y`` 落在上边距内 → 加 ``UP``；
        * ``y`` 落在下边距内 → 加 ``DOWN``。
        """
        dir_flag = Direction.NONE
        if x <= margin:
            dir_flag |= Direction.LEFT
        if x >= w - margin:
            dir_flag |= Direction.RIGHT
        if y <= margin:
            dir_flag |= Direction.UP
        if y >= h - margin:
            dir_flag |= Direction.DOWN
        return dir_flag


class WindowResizer(QObject):
    """给无边框窗口补上「拖拽移动 + 边缘缩放」。

    装法（两种等价）::

        # 1. 构造时直接绑定窗口（setup_flag=True 会顺手设成无边框窗口）
        WindowResizer(parent=window, window_=window)

        # 2. 先建、后装（窗口自己已经设过 FramelessWindowHint）
        resizer = WindowResizer(parent=window)
        resizer.install(window, setup_flag=False)
    """

    def __init__(self, parent: Optional[QObject] = None,
                 window_: Optional[QWidget] = None,
                 setup_flag: bool = True):
        super().__init__(parent)
        self.window_: Optional[QWidget] = window_
        self._margin = 12  # 边缘可缩放厚度
        self._title_bar_height = 36  # 拿不到标题栏控件时的兜底高度

        self.is_dragging = False
        self.last_direction: Union[Direction, int] = Direction.NONE

        if window_ is not None:
            self.install(window_, setup_flag)

    # ------------------------------------------------------------------ #
    # 可调参数
    # ------------------------------------------------------------------ #
    @property
    def margin(self) -> int:
        """边缘可缩放厚度（像素）。"""
        return self._margin

    @margin.setter
    def margin(self, value: int) -> None:
        self._margin = value

    @property
    def title_bar_height(self) -> int:
        """标题栏高度兜底线：窗口没暴露标题栏控件时，``y`` 不超过它就算按在标题栏上。"""
        return self._title_bar_height

    @title_bar_height.setter
    def title_bar_height(self, value: int) -> None:
        self._title_bar_height = value

    @property
    def win(self) -> QWidget:
        """被绑定的窗口；还没绑定时抛 :class:`RuntimeError`。"""
        if self.window_ is None:
            raise RuntimeError("WindowResizer window not yet bound.")
        return self.window_

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def install(self, window_: QWidget, setup_flag: bool = True) -> None:
        """把管理器装到 ``window_`` 上；``setup_flag`` 为真时顺手设成无边框。"""
        self.window_ = window_

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QApplication instance not created yet")
        app.installEventFilter(self)

        if not setup_flag:
            return

        window_.setMouseTracking(True)
        window_.setWindowFlag(Qt.WindowType.FramelessWindowHint)

    # ------------------------------------------------------------------ #
    # 几何判定
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_qt_edges(direction: int) -> Qt.Edge:
        """把 :class:`Direction` 位标志翻成 Qt 的边集合。"""
        edges = Qt.Edge(0)
        if direction & Direction.LEFT:
            edges |= Qt.Edge.LeftEdge
        if direction & Direction.RIGHT:
            edges |= Qt.Edge.RightEdge
        if direction & Direction.UP:
            edges |= Qt.Edge.TopEdge
        if direction & Direction.DOWN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _in_title_bar(self, local_pos) -> bool:
        """窗口内坐标是否落在标题栏区域（可以按住拖窗口的位置）。

        优先用窗口自己暴露的标题栏控件实测几何（``window.title_bar
        .action_bar``）：窗口标题、按钮之间的空白都算，工具栏不算（那里归
        工具栏的按钮与事件透传管）。拿不到标题栏控件（例如别的窗口类）时，
        退回 :attr:`title_bar_height` 这条兜底线。
        """
        action_bar = self._action_bar()
        if action_bar is not None:
            return action_bar.rect().contains(
                action_bar.mapFrom(self.win, local_pos))
        return local_pos.y() <= self._title_bar_height

    def _action_bar(self) -> Optional[QWidget]:
        """窗口暴露的操作栏控件（没有 / 不可见时返回 ``None``）。"""
        title_bar = getattr(self.win, "title_bar", None)
        action_bar = getattr(title_bar, "action_bar", None)
        if isinstance(action_bar, QWidget) and action_bar.isVisible():
            return action_bar
        return None

    def get_direction(self, event: QMouseEvent) -> int:
        """由鼠标位置（换算成窗口内坐标）算出缩放方向。"""
        pos = self.win.mapFromGlobal(event.globalPosition().toPoint())
        return Direction.from_mouse_pos(
            pos.x(), pos.y(),
            self.win.width(),
            self.win.height(),
            self.margin,
        )

    # ------------------------------------------------------------------ #
    # 光标
    # ------------------------------------------------------------------ #
    def change_mouse(self, direction: Direction | int) -> None:
        """按缩放方向换光标；与上次相同则跳过（避免每帧重设）。"""
        if direction == self.last_direction:
            return
        match direction:
            case Direction.UP | Direction.DOWN:
                self.set_cursor(Qt.CursorShape.SizeVerCursor)
            case Direction.LEFT | Direction.RIGHT:
                self.set_cursor(Qt.CursorShape.SizeHorCursor)
            case Direction.TOP_LEFT | Direction.BOTTOM_RIGHT:
                self.set_cursor(Qt.CursorShape.SizeFDiagCursor)
            case Direction.BOTTOM_LEFT | Direction.TOP_RIGHT:
                self.set_cursor(Qt.CursorShape.SizeBDiagCursor)
            case Direction.NONE:
                self.win.unsetCursor()

    def set_cursor(self, shape) -> None:
        self.win.setCursor(QCursor(shape))

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() not in (QEvent.Type.MouseMove,
                                QEvent.Type.MouseButtonPress,
                                QEvent.Type.MouseButtonRelease):
            return super().eventFilter(watched, event)

        # 只处理属于目标窗口的事件（应用级过滤器会看到所有窗口的事件）。
        if not isinstance(watched, QWidget):
            return super().eventFilter(watched, event)
        if watched.window() is not self.win:
            return super().eventFilter(watched, event)

        event: QMouseEvent = event  # type: ignore[assignment]

        match event.type():
            case QEvent.Type.MouseMove:
                if self.win.isMaximized():
                    self.win.unsetCursor()
                    return False
                if self.is_dragging:
                    return False
                direction = self.get_direction(event)
                self.change_mouse(direction)
                self.last_direction = direction
                return False

            case QEvent.Type.MouseButtonPress:
                if event.button() != Qt.MouseButton.LeftButton:
                    return super().eventFilter(watched, event)

                local_pos = self.win.mapFromGlobal(
                    event.globalPosition().toPoint()
                )

                # 交互控件自己处理点击，别当成拖拽。
                child = self.win.childAt(local_pos)
                if isinstance(child, _INTERACTIVE_TYPES):
                    return super().eventFilter(watched, event)

                handle = self.win.windowHandle()
                # noinspection unreachable-code
                if handle is None:
                    return super().eventFilter(watched, event)

                direction = self.get_direction(event)

                # ---- 最大化：按标题栏 → 还原 + 挪到光标下 + 拖拽 ----
                if self.win.isMaximized():
                    if not self._in_title_bar(local_pos):
                        return super().eventFilter(watched, event)

                    global_pos = event.globalPosition().toPoint()
                    screen = self.win.screen()
                    screen_width = (
                        screen.geometry().width() if screen else 1920
                    )
                    ratio = (
                        local_pos.x() / screen_width
                        if screen_width > 0 else 0.0
                    )

                    self.win.showNormal()

                    normal_width = self.win.width()
                    new_x = global_pos.x() - int(normal_width * ratio)
                    new_y = (
                            global_pos.y()
                            - self._margin
                            - self._title_bar_height // 2
                    )
                    self.win.move(new_x, new_y)

                    self.is_dragging = True
                    self.last_direction = Direction.NONE
                    # 延后一拍：showNormal() 可能让原生句柄失效。
                    QTimer.singleShot(0, handle.startSystemMove)
                    return True

                # ---- 普通窗口 ----
                if direction != Direction.NONE:
                    self.is_dragging = True
                    self.last_direction = direction
                    handle.startSystemResize(self._to_qt_edges(direction))
                    return True

                if self._in_title_bar(local_pos):
                    self.is_dragging = True
                    self.last_direction = Direction.NONE
                    handle.startSystemMove()
                    return True

                return super().eventFilter(watched, event)

            case QEvent.Type.MouseButtonRelease:
                self.is_dragging = False
                if self.get_direction(event) == Direction.NONE:
                    self.win.unsetCursor()
                return False

        return super().eventFilter(watched, event)
