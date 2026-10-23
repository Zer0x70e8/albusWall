#
""""""

from PySide6.QtWidgets import QMenu

from ..i18n import Retranslatable


class Menu(Retranslatable, QMenu):
    """标题栏「更多」菜单。

    文案集中在 :meth:`retranslate_ui`：语言切换时只改文字、不重建菜单
    —— 动作对象被外部信号连着，重建会把连接弄丢。
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 只声明结构：动作的文案由 retranslate_ui() 填
        self.source_action = self.addAction("")
        self.setting_action = self.addAction("")
        self.addSeparator()
        self.exit_action = self.addAction("")

        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """重新设置各菜单项文字。"""
        self.source_action.setText(self.tr("source"))
        self.setting_action.setText(self.tr("setting"))
        self.exit_action.setText(self.tr("exit"))
