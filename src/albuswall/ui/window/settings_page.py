#
"""设置页容器：浮层里的一块「模糊背景 + 关闭按钮 + 被动视图」。

它只负责三件**视图**的事：

1. 套与相册页 / 导入源页一致的模糊背景（:class:`BlurLabel`）；
2. 提供页面级关闭按钮（点击发 :attr:`close_requested`，由窗口收起浮层）；
3. 把 :class:`~albuswall.ui.setting.SettingView` 装进来，并把「进入页面时
   焦点落在哪」收口成 :meth:`focus_initial`。

数据 / 写回全部在 presenter（``SettingsPresenter``）里：本模块不 import
配置器、不认识 dump。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..i18n import Retranslatable
from ..setting import SettingView
from ..widgets import BlurLabel
from .utils import install_close_button

__all__ = ["SettingsPage"]


class SettingsPage(Retranslatable, BlurLabel):
    """``overlay`` 里的设置页。"""

    #: 用户点关闭按钮（窗口据此收起浮层）。
    close_requested = Signal()

    def __init__(
            self,
            target: QWidget | None = None,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, target=target, draw_label_content=False)
        self.setObjectName("SettingsPage")

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.close_button = install_close_button(self)
        self.close_button.clicked.connect(self.close_requested.emit)

        self.view = SettingView(self)
        self.main_layout.addWidget(self.close_button)
        self.main_layout.addWidget(self.view, 1)

        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新设置页面级文案（视图内部的文案由 dump 带下来）。"""
        self.close_button.setToolTip(self.tr("Close"))

    # ------------------------------------------------------------------ #
    # 焦点
    # ------------------------------------------------------------------ #
    def focus_initial(self) -> bool:
        """进入设置页时的焦点落点：第一个可编辑的叶子，否则关闭按钮。"""
        if self.view.focus_first():
            return True
        self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)
        return True
