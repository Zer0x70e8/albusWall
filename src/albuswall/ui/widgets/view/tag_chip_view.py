#
"""区分「点主体」与「点 X」的 QListView。

只依赖 :class:`TagChipDelegate` 与 Qt 的 role 约定，不认识任何具体 model：

* ``DisplayRole``  —— chip 文本；
* :data:`TAG_ROLE` —— 该 chip 的标识（推荐放带 ``uuid`` 属性的对象；
  直接放字符串 / UUID 也支持，会 ``str()`` 后抛给外面）。

键盘行为（与鼠标等价）：

* ``←`` / ``→``：在当前 chip 之间移动；
* ``Enter`` / ``Return``：激活当前 chip；
* ``Delete`` / ``Backspace``：移除当前 chip。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QListView

from ...model.roles import TagRole
from ..widget.tag_chip_delegate import TagChipDelegate

__all__ = ["TagChipView", "TAG_ROLE"]

#: chip 承载的标识（uuid / 带 uuid 属性的对象）所在 role。
#: 单一来源在 ``albuswall.ui.model.roles.TagRole``，这里只是别名。
TAG_ROLE = int(TagRole.Tag)


class TagChipView(QListView):
    chip_activated = Signal(str)         # uuid
    chip_remove_requested = Signal(str)  # uuid

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(TagChipDelegate(self))
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        # QListView 自带的 Enter/Return → activated 已经够用
        self.activated.connect(self._on_activated)

    # -------------------------------------------------------------- 数据
    def bind_model(self, model, delegate=None) -> "TagChipView":
        """直接接收一份模型（通常为 :class:`~albuswall.ui.model.TagChipModel`）。

        :param delegate: 委托；``None`` 时保留自带的
            :class:`~albuswall.ui.widgets.TagChipDelegate`。
        :return: ``self``，便于链式调用。
        """
        self.setModel(model)
        if delegate is not None:
            self.setItemDelegate(delegate)
        return self

    # -------------------------------------------------------------- 鼠标
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                rect = self.visualRect(index)
                delegate = self.itemDelegate()
                if not isinstance(delegate, TagChipDelegate):
                    super().mouseReleaseEvent(event)
                    return
                if delegate.remove_button_rect(rect).contains(
                        event.position().toPoint()):
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

    @staticmethod
    def uuid_of(index: QModelIndex) -> Optional[str]:
        """取 chip 的 uuid 字符串；无名标识时返回 ``None``。"""
        if not index.isValid():
            return None
        value = index.data(TAG_ROLE)
        if value is None:
            return None
        uid = getattr(value, "uuid", value)
        return str(uid) if uid is not None else None

    def _activate_index(self, index: QModelIndex) -> None:
        uuid = self.uuid_of(index)
        if uuid:
            self.chip_activated.emit(uuid)

    def _remove_index(self, index: QModelIndex) -> None:
        uuid = self.uuid_of(index)
        if uuid:
            self.chip_remove_requested.emit(uuid)
