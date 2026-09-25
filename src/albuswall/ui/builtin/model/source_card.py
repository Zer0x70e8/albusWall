#
""""""

from dataclasses import dataclass, field

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Qt
)


@dataclass
class CardItem:
    title: str = ""
    description: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)


class CardListModel(QAbstractListModel):
    """卡片列表模型。"""

    TitleRole = int(Qt.ItemDataRole.UserRole) + 1
    DescriptionRole = int(Qt.ItemDataRole.UserRole) + 2
    PathRole = int(Qt.ItemDataRole.UserRole) + 3
    TagsRole = int(Qt.ItemDataRole.UserRole) + 4

    # ★ 类级别常量，避免每次都重新构造 dict
    _ROLE_NAMES = {
        TitleRole: b"title",
        DescriptionRole: b"description",
        PathRole: b"path",
        TagsRole: b"tags",
    }

    def __init__(self, items: list[CardItem] | None = None, parent=None):
        super().__init__(parent)
        self._items: list[CardItem] = list(items) if items else []

    # ---------- 只读接口 ----------
    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        items = self._items
        if row < 0 or row >= len(items):
            return None

        item = items[row]

        if role == Qt.ItemDataRole.DisplayRole or role == self.TitleRole:
            return item.title
        if role == self.DescriptionRole:
            return item.description
        if role == self.PathRole:
            return item.path
        if role == self.TagsRole:
            return list(item.tags)
        if role == Qt.ItemDataRole.ToolTipRole:
            # 直接拼接，避免中间 list 构造
            return "\n".join(p for p in (item.title, item.description, item.path) if p)
        return None

    # noinspection method-overriding
    def roleNames(self) -> dict[int, bytes]:
        return self._ROLE_NAMES

    def flags(self, index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        # noinspection unsupported-operator
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ---------- 编辑接口 ----------
    def set_items(self, items: list[CardItem]):
        # ★ 若新旧内容相同则跳过，避免无意义的重置
        new = list(items)
        if len(new) == len(self._items) and all(
                a == b for a, b in zip(self._items, new)):
            return
        self.beginResetModel()
        self._items = new
        self.endResetModel()

    def add_item(self, item: CardItem):
        self.add_items([item])

    def add_items(self, items: list[CardItem]):
        if not items:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(items) - 1)
        self._items.extend(items)
        self.endInsertRows()

    def remove_rows(self, row: int, count: int = 1) -> bool:
        if row < 0 or count <= 0 or row + count > len(self._items):
            return False
        self.beginRemoveRows(QModelIndex(), row, row + count - 1)
        del self._items[row:row + count]
        self.endRemoveRows()
        return True

    def clear(self):
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    def item_at(self, row: int) -> CardItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def update_item(self, row: int, item: CardItem) -> bool:
        if not (0 <= row < len(self._items)):
            return False
        if self._items[row] == item:
            return False  # ★ 内容未变就不发信号
        self._items[row] = item
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx)
        return True
