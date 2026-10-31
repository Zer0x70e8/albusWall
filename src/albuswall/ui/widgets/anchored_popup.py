#
"""锚定到某个 widget 下方弹出的 Popup，内部处理 Wayland 定位兼容。"""

from PySide6.QtCore import Qt, QPoint, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QFrame
, QWidget)


class AnchoredPopup(QFrame):
    """锚定到 anchor widget 的弹出层。

    相比 Qt.WindowType.Popup，额外做两件事：
    1. 显示前把自身设为 anchor 所在顶层窗口的 transient parent；
    2. 在 Wayland 下通过 Qt 私有属性设置 popup 的 anchor rect / gravity。
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setLineWidth(1)

        self.setMinimumWidth(220)
        self.setMinimumHeight(260)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)

    def show_under(self, anchor: QWidget):
        popup_window = self.windowHandle()
        parent_window = anchor.window()
        if popup_window and parent_window and parent_window.windowHandle():
            popup_window.setTransientParent(parent_window.windowHandle())

        self._prepare_wayland_positioning(anchor)
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height())))
        self.show()

    # noinspection unsupported-operator
    def _prepare_wayland_positioning(self, anchor: QWidget):
        if "wayland" not in QGuiApplication.platformName().lower():
            return
        if not self.windowHandle():
            return

        window = self.windowHandle()
        parent_window = anchor.window()
        top_left_in_parent = anchor.mapTo(parent_window, QPoint(0, 0))
        anchor_rect = QRect(top_left_in_parent, anchor.size())

        window.setProperty("_q_waylandPopupAnchorRect", anchor_rect)
        window.setProperty("_q_waylandPopupAnchor",
                           Qt.Edge.BottomEdge | Qt.Edge.LeftEdge)
        window.setProperty("_q_waylandPopupGravity",
                           Qt.Edge.BottomEdge | Qt.Edge.LeftEdge)
        window.setProperty("_q_waylandPopupConstraintAdjustment",
                           (0x1 | 0x2 | 0x4 | 0x8))
