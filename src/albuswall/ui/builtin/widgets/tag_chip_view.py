#
"""区分「点主体」与「点 X」的 QListView。

键盘行为（与鼠标等价，供纯键盘操作使用）：

* ``←`` / ``→``：在当前标签 chip 之间移动；
* ``Enter`` / ``Return``：激活当前 chip（按标签过滤相册）；
* ``Delete`` / ``Backspace``：从当前资产上移除该标签；
* 方向键移动时的选择高亮由 :class:`TagChipDelegate` 绘制。
"""

from __future__ import annotations

from typing import Optional

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
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        # QListView 自带的 Enter/Return → activated 已经够用
        self.activated.connect(self._on_activated)

    # -------------------------------------------------------------- 鼠标
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                rect = self.visualRect(index)
                delegate: TagChipDelegate = self.itemDelegate()
                if delegate.remove_button_rect(rect).contains(
                    event.position().toPoint()
                ):
                    self._remove_index(index)
                else:
                    self._activate_index(index)
                return
        super().mouseReleaseEvent(event)

    # -------------------------------------------------------------- 键盘
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            index = self.currentIndex()
            if index.isValid():
                self._remove_index(index)
                event.accept()
                return
        super().keyPressEvent(event)

    # -------------------------------------------------------------- 内部
    def _on_activated(self, index: QModelIndex) -> None:
        self._activate_index(index)

    def _uuid_of(self, index: QModelIndex) -> Optional[str]:
        if not index.isValid():
            return None
        tag = index.data(TagChipModel.TagRole)
        return str(tag.uuid) if tag is not None else None

    def _activate_index(self, index: QModelIndex) -> None:
        uuid = self._uuid_of(index)
        if uuid:
            self.chip_activated.emit(uuid)

    def _remove_index(self, index: QModelIndex) -> None:
        uuid = self._uuid_of(index)
        if uuid:
            self.chip_remove_requested.emit(uuid)
