#
"""标签模型：TagBar 的 chip 列表 + 层级标签树。

两者都**不认识 DTO**：只要对象带 ``uuid``（``str`` / ``UUID`` 都行），
再按需带 ``name`` / ``display_name`` / ``description``，就能直接喂进来。
需要自己造一棵树时用 :class:`TagNode`；喂领域层现成的树对象也可以，
:meth:`TagTreeModel.set_roots` 会自动按属性名把它们规整成节点。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt

from .base import EntryListModel
from .roles import TagRole

__all__ = [
    "TagChipModel",
    "TagNode",
    "TagTreeModel",
    "tag_description",
    "tag_name",
    "tag_uuid",
]

_DisplayRole = int(Qt.ItemDataRole.DisplayRole)
_ToolTipRole = int(Qt.ItemDataRole.ToolTipRole)
_TagRole = int(TagRole.Tag)


# --------------------------------------------------------------------------- #
# 鸭子类型读取
# --------------------------------------------------------------------------- #
def tag_uuid(tag: Any) -> str:
    """取标签标识的字符串口径（缺失时为空串）。"""
    uid = getattr(tag, "uuid", None)
    return "" if uid is None else str(uid)


def tag_name(tag: Any) -> str:
    """取标签显示名：``display_name`` 优先，其次 ``name``/``title``。"""
    for attribute in ("display_name", "name", "title"):
        value = getattr(tag, attribute, None)
        if value:
            return str(value)
    return ""


def tag_description(tag: Any) -> str:
    """取标签描述（缺失时为空串）。"""
    value = getattr(tag, "description", None)
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- #
# chip 列表
# --------------------------------------------------------------------------- #
class TagChipModel(EntryListModel[Any]):
    """TagBar 的 chip 列表模型（一行一个标签）。

    Roles::

        DisplayRole / ToolTipRole   → 显示名（tooltip 用描述，缺省回退显示名）
        TagRole.Tag                 → 标签对象本身
    """

    def data(
            self,
            index: QModelIndex,
            role: int = _DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        tag = self.entry_at(index.row())
        if tag is None:
            return None

        role = int(role)
        if role == _DisplayRole:
            return tag_name(tag)
        if role == _ToolTipRole:
            return tag_description(tag) or tag_name(tag)
        if role == _TagRole:
            return tag
        return None

    # -------- 装载 --------
    def set_tags(self, tags: Iterable[Any]) -> None:
        """整体替换标签序列（按 uuid 去重，保留首次出现的顺序）。"""
        self.set_entries(_unique_tags(tags))

    # -------- 查询 --------
    def tag_at(self, row: int) -> Any | None:
        return self.entry_at(row)

    def tags(self) -> list[Any]:
        return self.entries()

    def uuid_at(self, row: int) -> UUID | str | None:
        tag = self.entry_at(row)
        if tag is None:
            return None
        uid = getattr(tag, "uuid", None)
        if isinstance(uid, UUID):
            return uid
        return str(uid) if uid is not None else None

    def row_of(self, uuid: Any) -> int:
        """返回标识对应的行号；找不到时返回 ``-1``。"""
        target = str(uuid)
        for row, tag in enumerate(self._entries):
            if tag_uuid(tag) == target:
                return row
        return -1

    def index_of(self, uuid: Any) -> QModelIndex:
        row = self.row_of(uuid)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def has_tag(self, uuid: Any) -> bool:
        return self.row_of(uuid) >= 0

    # -------- 修改 --------
    def remove_at(self, row: int) -> Any | None:
        """删除一行并返回被删的标签；越界返回 ``None``。"""
        tag = self.entry_at(row)
        if tag is None:
            return None
        self.remove_rows(row, 1)
        return tag

    def remove_batch(self, uuids: Iterable[Any]) -> int:
        """按标识批量删除，返回实际删除数量。"""
        targets = {str(uuid) for uuid in uuids}
        if not targets:
            return 0
        keep = [tag for tag in self._entries if tag_uuid(tag) not in targets]
        removed = len(self._entries) - len(keep)
        if removed:
            self.set_entries(keep, skip_if_equal=False)
        return removed


def _unique_tags(tags: Iterable[Any]) -> list[Any]:
    """按 uuid 去重（uuid 为空时不去重，保持原样）。"""
    seen: set[str] = set()
    result: list[Any] = []
    for tag in tags:
        uid = tag_uuid(tag)
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        result.append(tag)
    return result


# --------------------------------------------------------------------------- #
# 标签树
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TagNode:
    """标签树的一个节点（可自己造，也可由领域对象转换而来）。"""

    uuid: str = ""
    name: str = ""
    description: str = ""
    children: list["TagNode"] = field(default_factory=list)
    #: 原始标签对象；不给时用 ``uuid`` / ``name`` 现造一个只读替身。
    tag: Any = None


@dataclass(slots=True)
class _Row:
    """内部节点：包装 :class:`TagNode` + 父指针 + 行号。"""

    node: TagNode
    parent: "_Row | None" = None
    children: list["_Row"] = field(default_factory=list)
    row: int = 0

    @property
    def tag(self) -> Any:
        return self.node.tag if self.node.tag is not None else self.node


class TagTreeModel(QAbstractItemModel):
    """层级标签树模型。

    Roles::

        DisplayRole / ToolTipRole   → 显示名（tooltip 用描述，缺省回退显示名）
        TagRole.Tag                 → 标签对象（``TagNode.tag`` 或节点本身）
    """

    def __init__(self, roots: Iterable[Any] | None = None, parent: Any = None):
        super().__init__(parent)
        self._roots: list[_Row] = []
        #: uuid(str) → 内部节点；上行回溯与定位都靠它，避免全树搜索
        self._by_uuid: dict[str, _Row] = {}
        if roots is not None:
            self.set_roots(roots)

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def set_roots(self, roots: Iterable[Any]) -> None:
        """整体替换根节点序列（``TagNode`` 或任意领域树对象）。"""
        self.beginResetModel()
        self._roots = []
        self._by_uuid = {}
        for row, root in enumerate(roots):
            self._roots.append(self._build(_to_node(root), parent=None, row=row))
        self.endResetModel()

    def set_tree(self, roots: Iterable[TagNode]) -> None:
        """:meth:`set_roots` 的别名（强调入参是 ``TagNode``）。"""
        self.set_roots(roots)

    def clear(self) -> None:
        if not self._roots:
            return
        self.beginResetModel()
        self._roots = []
        self._by_uuid = {}
        self.endResetModel()

    def _build(self, node: TagNode, parent: _Row | None, row: int) -> _Row:
        wrapped = _Row(node=node, parent=parent, row=row)
        if node.uuid:
            self._by_uuid.setdefault(node.uuid, wrapped)
        for index, child in enumerate(node.children):
            wrapped.children.append(self._build(child, wrapped, index))
        return wrapped

    # ------------------------------------------------------------------ #
    # 便捷查询
    # ------------------------------------------------------------------ #
    def tag_at(self, index: QModelIndex) -> Any | None:
        """索引处的标签对象；无效索引返回 ``None``。"""
        if not index.isValid():
            return None
        return index.internalPointer().tag

    def uuid_at(self, index: QModelIndex) -> str:
        """索引处的标签标识（字符串口径）；无效索引返回空串。"""
        tag = self.tag_at(index)
        return tag_uuid(tag) if tag is not None else ""

    def index_of(self, uuid: Any) -> QModelIndex:
        """标识对应的索引；找不到时返回无效索引。"""
        wrapped = self._by_uuid.get(str(uuid))
        if wrapped is None:
            return QModelIndex()
        return self.createIndex(wrapped.row, 0, wrapped)

    def path_of(self, uuid: Any) -> list[str]:
        """从根到该节点的标识路径（找不到时返回空列表）。"""
        wrapped = self._by_uuid.get(str(uuid))
        if wrapped is None:
            return []
        path: list[str] = []
        cursor: _Row | None = wrapped
        while cursor is not None:
            path.append(tag_uuid(cursor.node))
            cursor = cursor.parent
        path.reverse()
        return path

    def node_of(self, uuid: Any) -> TagNode | None:
        wrapped = self._by_uuid.get(str(uuid))
        return wrapped.node if wrapped is not None else None

    # ------------------------------------------------------------------ #
    # QAbstractItemModel
    # ------------------------------------------------------------------ #
    def index(
            self,
            row: int,
            column: int,
            parent: QModelIndex = QModelIndex(),
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        siblings = (
            parent.internalPointer().children if parent.isValid()
            else self._roots
        )
        if row >= len(siblings):
            return QModelIndex()
        return self.createIndex(row, column, siblings[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        wrapped: _Row = index.internalPointer()
        above = wrapped.parent
        if above is None:
            return QModelIndex()
        return self.createIndex(above.row, 0, above)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        if parent.isValid():
            return len(parent.internalPointer().children)
        return len(self._roots)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def data(
            self,
            index: QModelIndex,
            role: int = _DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        wrapped: _Row = index.internalPointer()
        tag = wrapped.tag

        role = int(role)
        if role == _DisplayRole:
            return tag_name(tag) or tag_name(wrapped.node)
        if role == _ToolTipRole:
            return (
                tag_description(tag)
                or tag_description(wrapped.node)
                or tag_name(tag)
            )
        if role == _TagRole:
            return tag
        return None


# --------------------------------------------------------------------------- #
# 领域对象 → TagNode
# --------------------------------------------------------------------------- #
def _to_node(value: Any) -> TagNode:
    """把任意树节点（``TagNode`` / 带 ``tag``+``children`` 的对象）规整成节点。"""
    if isinstance(value, TagNode):
        return value

    # 领域层的树节点：自身是「包装」，真正的标签在 .tag 里
    tag = getattr(value, "tag", None)
    source = tag if tag is not None else value

    children = getattr(value, "children", None) or getattr(source, "children", None)
    node = TagNode(
        uuid=tag_uuid(source),
        name=tag_name(source),
        description=tag_description(source),
        children=[_to_node(child) for child in (children or ())],
        tag=source,
    )
    return node
