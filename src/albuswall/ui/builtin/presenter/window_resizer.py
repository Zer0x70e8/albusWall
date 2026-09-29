#
"""Self-implemented window resizing functionality"""

from typing import Optional, Union
from enum import IntFlag

from PySide6.QtGui import QMouseEvent, QCursor
from PySide6.QtCore import QObject, QEvent, Qt, QTimer
from PySide6.QtWidgets import QWidget, QApplication, QAbstractButton, QSlider, QComboBox

__all__ = ["WindowResizer", "Direction"]


class Direction(IntFlag):
    """Bit flags for resize directions. Combine using | operator."""
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
        """
        Determine resize direction from local mouse position.

        - x within left margin  -> add LEFT flag.
        - x within right margin -> add RIGHT flag.
        - y within top margin   -> add UP flag.
        - y within bottom margin-> add DOWN flag.
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
    """
    Adds frameless window resizing and dragging.

    How it works:
    - Listens to global mouse events via event filter.
    - When mouse is near window edges, changes cursor shape.
    - On left button press near edges, delegates to
      ``windowHandle().startSystemResize(edges)``.
    - On left button press boot the title bar, delegates to
      ``windowHandle().startSystemMove()``.
    - Special handling: if window is maximized, a click boot the title bar
      restores it, moves it under the mouse cursor and starts dragging.
    """

    def __init__(self, parent: Optional[QObject] = None,
                 window_: Optional[QWidget] = None,
                 setup_flag: bool = True):
        super().__init__(parent)
        self.window_: Optional[QWidget] = window_
        self._margin = 12  # Edge thickness for resizing
        self._title_bar_height = 36  # Fallback if no title_bar widget

        self.is_dragging = False
        self.last_direction: Union[Direction, int] = Direction.NONE

        if window_ is not None:
            self.install(window_, setup_flag)

    @property
    def margin(self) -> int:
        return self._margin

    @margin.setter
    def margin(self, value: int) -> None:
        self._margin = value

    @property
    def title_bar_height(self) -> int:
        return self._title_bar_height

    @title_bar_height.setter
    def title_bar_height(self, value: int) -> None:
        self._title_bar_height = value

    @property
    def win(self) -> QWidget:
        if self.window_ is None:
            raise RuntimeError("WindowResizer window not yet bound.")
        return self.window_

    def install(self, window_: QWidget, setup_flag: bool = True) -> None:
        """Attach resizer to a window. If setup_flag, makes window frameless."""
        self.window_ = window_

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QApplication instance not created yet")
        app.installEventFilter(self)

        if not setup_flag:
            return

        window_.setMouseTracking(True)
        window_.setWindowFlag(Qt.WindowType.FramelessWindowHint)

    @staticmethod
    def _to_qt_edges(direction: int) -> Qt.Edge:
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
        """Check whether a window-local position is inside the title bar."""
        # tb = getattr(self.win, "title_bar", None)
        # tb: QWidget
        # if tb is not None and tb.isVisible():
        #     return tb.rect().contains(tb.mapFrom(self.win, local_pos))
        return local_pos.y() <= self._title_bar_height

    def get_direction(self, event: QMouseEvent) -> int:
        """Get resize direction flags from mouse position relative to window."""
        pos = self.win.mapFromGlobal(event.globalPosition().toPoint())
        return Direction.from_mouse_pos(
            pos.x(), pos.y(),
            self.win.width(),
            self.win.height(),
            self.margin,
        )

    def change_mouse(self, direction: Direction | int) -> None:
        """Update cursor shape based boot resize direction. Skips if same as last."""
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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() not in (QEvent.Type.MouseMove,
                                QEvent.Type.MouseButtonPress,
                                QEvent.Type.MouseButtonRelease):
            return super().eventFilter(watched, event)

        # Only handle events belonging to the target window.
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

                # Let interactive controls handle their own clicks.
                child = self.win.childAt(local_pos)
                if isinstance(child, (QAbstractButton, QSlider, QComboBox)):
                    return super().eventFilter(watched, event)
                # if child is not None and child is not self.win:
                #     return super().eventFilter(watched, event)

                handle = self.win.windowHandle()
                # noinspection unreachable-code
                if handle is None:
                    return super().eventFilter(watched, event)

                direction = self.get_direction(event)

                # ---- Maximized: click boot title bar restores + drags ----
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
                    # Defer: showNormal() may invalidate the native handle.
                    QTimer.singleShot(0, handle.startSystemMove)
                    return True

                # ---- Normal window ----
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
