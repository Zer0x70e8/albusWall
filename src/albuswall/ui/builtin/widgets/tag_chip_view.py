#
"""区分「点主体」与「点 X」的 QListView。"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QListView

from albuswall.ui.builtin.delegate.tag_chip_delegate import TagChipDelegate
from albuswall.ui.builtin.model.tag_chip_model import TagChipModel


class TagChipView(QListView):
    chip_activated = Signal(str)   # uuid
    chip_remove_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(TagChipDelegate(self))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                rect = self.visualRect(index)
                delegate: TagChipDelegate = self.itemDelegate()
                if delegate.remove_button_rect(rect).contains(
                    event.position().toPoint()
                ):
                    uuid = index.data(TagChipModel.TagRole).uuid
                    self.chip_remove_requested.emit(str(uuid))
                else:
                    uuid = index.data(TagChipModel.TagRole).uuid
                    self.chip_activated.emit(str(uuid))
                return
        super().mouseReleaseEvent(event)