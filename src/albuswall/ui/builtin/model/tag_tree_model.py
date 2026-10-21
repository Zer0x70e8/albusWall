#
"""TagTree（层级标签树）模型。

内部节点包装 TagTreeNode + parent 指针，避免上行回溯时依赖外部索引。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt

from albuswall.services.tag import TagTreeNode


@dataclass
class _Node:
    node: TagTreeNode
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    row: int = 0


class TagTreeModel(QAbstractItemModel):
    TagRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._roots: list[_Node] = []
        # 键统一为 canonical str，避免 UUID / str 两种口径混用
        self._by_uuid: dict[str, _Node] = {}

    # ---- 装载 ----
    def set_roots(self, roots: list[TagTreeNode]) -> None:
        self.beginResetModel()
        self._roots = []
        self._by_uuid = {}
        for row, root in enumerate(roots):
            self._roots.append(self._build(root, parent=None, row=row))
        self.endResetModel()

    def _build(self, node: TagTreeNode, parent: _Node | None, row: int) -> _Node:
        n = _Node(node=node, parent=parent, row=row)
        self._by_uuid[str(node.uuid)] = n
        for i, child in enumerate(node.children):
            n.children.append(self._build(child, parent=n, row=i))
        return n

    # ---- 便捷查询 ----
    def uuid_at(self, index: QModelIndex) -> UUID | None:
        if not index.isValid():
            return None
        return index.internalPointer().node.uuid

    def index_of(self, uuid: "UUID | str") -> QModelIndex:
        n = self._by_uuid.get(str(uuid))
        if n is None:
            return QModelIndex()
        if n.parent is None:
            return self.createIndex(n.row, 0, n)
        return self.createIndex(n.row, 0, n)

    # ---- QAbstractItemModel ----
    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if parent.isValid():
            parent_node: _Node = parent.internalPointer()
            if row >= len(parent_node.children):
                return QModelIndex()
            return self.createIndex(row, column, parent_node.children[row])
        if row >= len(self._roots):
            return QModelIndex()
        return self.createIndex(row, column, self._roots[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: _Node = index.internalPointer()
        parent_node = node.parent
        if parent_node is None:
            return QModelIndex()
        return self.createIndex(parent_node.row, 0, parent_node)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        if parent.isValid():
            return len(parent.internalPointer().children)
        return len(self._roots)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: _Node = index.internalPointer()
        tag = node.node.tag
        if role == Qt.ItemDataRole.DisplayRole:
            return tag.display_name or tag.name
        if role == self.TagRole:
            return tag
        if role == Qt.ItemDataRole.ToolTipRole:
            return tag.description or tag.name
        return None
