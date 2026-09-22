from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QMainWindow, QToolButton, QWidget,
)


# noinspection pep8-naming
class _DockTitleBar(QWidget):
    """自定义标题栏：停靠 / 浮动时都可见，并带一个关闭按钮。"""

    close_requested = Signal()

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("StackStationDockTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 4, 3)
        layout.setSpacing(4)

        self._label = QLabel(title, self)
        self._label.setObjectName("StackStationDockTitleBarLabel")
        layout.addWidget(self._label, 1)

        self._close = QToolButton(self)
        self._close.setObjectName("StackStationDockCloseButton")
        self._close.setText("\u2715")
        self._close.setAutoRaise(True)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(self.close_requested.emit)
        layout.addWidget(self._close)

    def setTitle(self, title: str) -> None:
        self._label.setText(title)


class _ClosableDock(QDockWidget):
    """用户点关闭时：只隐藏 + 发信号，不销毁自己。"""

    closed = Signal()

    def closeEvent(self, event):
        # 交给 Qt 走标准隐藏流程（它会正确维护 dock area 状态），
        # WA_DeleteOnClose=False 保证对象不被销毁。
        # print("[dock closeEvent]")  # ← 加这个
        # print("WA_DeleteOnClose =",
        #       self.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose))
        event.accept()
        event.accept()
        self.closed.emit()


class StackStation(QMainWindow):
    widget_closed = Signal()
    dock_floated = Signal()
    dock_docked = Signal()

    def __init__(self, parent=None,
                 area=Qt.DockWidgetArea.LeftDockWidgetArea):
        super().__init__(parent)
        self._area = area
        self._dock: QDockWidget | None = None
        self._title_bar: _DockTitleBar | None = None

        dummy = QWidget(self)
        dummy.setFixedSize(0, 0)
        self.setCentralWidget(dummy)
        self.setMinimumSize(0, 0)
        self.setContentsMargins(0, 0, 0, 0)

    def set_widget(self, widget: QWidget, title: str = "") -> QDockWidget:
        if self._dock is None:
            dock = _ClosableDock(self)
            dock.setObjectName("StackStationDock")
            dock.setAllowedAreas(self._area)
            dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable  # type: ignore
                | QDockWidget.DockWidgetFeature.DockWidgetClosable  # type: ignore
            )
            dock.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            dock.closed.connect(self.widget_closed)

            tb = _DockTitleBar(title, dock)
            tb.close_requested.connect(dock.close)
            dock.setTitleBarWidget(tb)
            self._title_bar = tb

            self.addDockWidget(self._area, dock)
            self._dock = dock
        else:
            dock = self._dock

        if self._title_bar is not None:
            self._title_bar.setTitle(title)
        dock.setWindowTitle(title)

        old = dock.widget()
        if old is not None and old is not widget:
            old.setParent(None)

        dock.setWidget(widget)
        return dock

    def showEvent(self, event):
        super().showEvent(event)
        # 关键：StackStation 再次可见时，恢复被关掉的 dock
        if self._dock is not None and not self._dock.isVisible():
            # 万一 Qt 已经把它从 dock area 里摘掉，重新挂回去
            if self.dockWidgetArea(self._dock) == \
                    Qt.DockWidgetArea.NoDockWidgetArea:
                self.addDockWidget(self._area, self._dock)
            self._dock.show()

    @property
    def dock(self) -> QDockWidget | None:
        return self._dock

    def show_dock(self) -> None:
        """恢复被 close 掉的 dock。"""
        if self._dock is None:
            return
        if self.dockWidgetArea(self._dock) == \
                Qt.DockWidgetArea.NoDockWidgetArea:
            self.addDockWidget(self._area, self._dock)
        self._dock.show()

    def _install_dock_signals(self, dock: QDockWidget) -> None:
        dock.topLevelChanged.connect(
            lambda floating:
            (self.dock_floated if floating else self.dock_docked).emit()
        )
