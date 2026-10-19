#
"""TagBar chip 列表模型。"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from albuswall.dto.tag import Tag


class TagChipModel(QAbstractListModel):
    TagRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tags: list[Tag] = []

    # ---- 数据装载 ----
    def set_tags(self, tags: list[Tag]) -> None:
        self.beginResetModel()
        self._tags = list(tags)
        self.endResetModel()

    def remove_at(self, row: int) -> Tag | None:
        if not (0 <= row < len(self._tags)):
            return None
        self.beginRemoveRows(QModelIndex(), row, row)
        tag = self._tags.pop(row)
        self.endRemoveRows()
        return tag

    def tags(self) -> list[Tag]:
        return list(self._tags)

    def uuid_at(self, row: int) -> str | None:
        if 0 <= row < len(self._tags):
            return self._tags[row].uuid
        return None

    def index_of(self, uuid: str) -> QModelIndex:
        for row, t in enumerate(self._tags):
            if t.uuid == uuid:
                return self.index(row, 0)
        return QModelIndex()

    # ---- QAbstractListModel ----
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._tags)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        tag = self._tags[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return tag.display_name or tag.name
        if role == self.TagRole:
            return tag
        if role == Qt.ItemDataRole.ToolTipRole:
            return tag.description or tag.name
        return None
