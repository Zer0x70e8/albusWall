#
"""标题栏「更多」菜单。

只声明结构：动作对象由外部接线（本组件不认识任何业务）。

:meth:`Menu.retranslate_ui` 只改文字、不重建菜单 —— 动作对象被外部信号
连着，重建会把连接弄丢。
"""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QWidget

from ..i18n import Retranslatable

__all__ = ["Menu"]


class Menu(Retranslatable, QMenu):
    """标题栏「更多」菜单。"""

    # ---- 子控件声明（类型注解） ----
    source_action: QAction
    setting_action: QAction
    exit_action: QAction

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # 只声明结构：动作的文案由 retranslate_ui() 填
        self.source_action = self.addAction("")
        self.setting_action = self.addAction("")
        self.addSeparator()
        self.exit_action = self.addAction("")

        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新设置各菜单项文字。"""
        self.source_action.setText(self.tr("导入源"))
        self.setting_action.setText(self.tr("设置"))
        self.exit_action.setText(self.tr("退出"))
