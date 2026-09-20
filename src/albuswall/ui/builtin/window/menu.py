#
""""""

from PySide6.QtWidgets import QMenu


class Menu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.source_action = self.addAction(self.tr("source"))
        self.setting_action = self.addAction(self.tr("setting"))
        self.addSeparator()
        self.exit_action = self.addAction(self.tr("exit"))
