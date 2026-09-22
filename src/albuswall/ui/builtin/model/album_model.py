#
"""UI 展示模型：专辑网格的 QAbstractListModel。

- 属于 UI 层，不依赖 vo / 数据库。
- 只依赖 QtCore / QtGui，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtGui import QPixmap


class AlbumRole(IntEnum):
    """自定义 item data role。delegate / view 通过它取数据。"""
    Uuid = Qt.ItemDataRole.UserRole + 1
    Pixmap = Qt.ItemDataRole.UserRole + 2


@dataclass
class AlbumEntry:
    uuid: str
    pixmap: QPixmap | None = None


# noinspection pep8-naming
class AlbumModel(QAbstractListModel):
    """专辑列表模型。

    - 只持有数据，不创建任何 widget。
    - 增删改都走 Qt 的 begin/end 接口，绑定的 view 会自动同步。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[AlbumEntry] = []

    # ---------- QAbstractListModel 必须实现 ----------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._items)):
            return None
        entry = self._items[row]
        if role == AlbumRole.Uuid:
            return entry.uuid
        if role == AlbumRole.Pixmap:
            return entry.pixmap
        return None

    # ---------- 便捷 API ----------
    def append(self, uuid: str, pixmap: QPixmap | None = None) -> int:
        """追加一项，返回其 row。"""
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(AlbumEntry(uuid, pixmap))
        self.endInsertRows()
        return row

    def setPixmap(self, row: int, pixmap: QPixmap) -> None:
        if not (0 <= row < len(self._items)):
            return
        self._items[row].pixmap = pixmap
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [int(AlbumRole.Pixmap)])

    def uuid_at(self, row: int) -> str:
        if 0 <= row < len(self._items):
            return self._items[row].uuid
        return ""

    def pixmap_at(self, row: int) -> QPixmap | None:
        if 0 <= row < len(self._items):
            return self._items[row].pixmap
        return None

    def clear(self) -> None:
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()
