#
"""通用「条目列表」模型基类。

相册、资产、导入源卡片、导航条本质上是同一个东西：**一串条目 + 一张
role → 字段 的映射表**。与其每个模型重写一遍 ``rowCount`` / ``data`` /
``beginInsertRows`` 样板代码，不如把公共部分收敛到这里，子类只做两件事：

1. 声明 ``ROLE_FIELDS``（role → 条目属性名；
   ``DECORATION_FIELD`` / ``DISPLAY_FIELD`` / ``TOOLTIP_FIELD`` 是它们的
   常用快捷方式）；
2. 按需提供业务级查询（``uuid_at`` / ``row_of`` …）。

条目可以是 dataclass、DTO、``types.SimpleNamespace``，也可以是 ``dict``：
``data()`` 先按属性取，取不到再按 mapping 取，所以 UI 模型不认识任何
具体的领域类型。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, ClassVar, Generic, TypeVar

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

__all__ = ["EntryListModel", "attr_of"]

EntryT = TypeVar("EntryT")

#: ``data()`` 里 ``role`` 的整型口径（Qt 传进来的永远是 int）。
_DisplayRole = int(Qt.ItemDataRole.DisplayRole)
_ToolTipRole = int(Qt.ItemDataRole.ToolTipRole)
_DecorationRole = int(Qt.ItemDataRole.DecorationRole)


def attr_of(entry: object, name: str | None) -> Any:
    """按属性名从条目取值；``dict`` / 其它 ``Mapping`` 走键查找。

    取不到时返回 ``None``（而不是抛异常）：``data()`` 是给视图回调的热路径，
    一个字段缺失不该让整格绘制炸掉。
    """
    if not name:
        return None
    if isinstance(entry, Mapping):
        return entry.get(name)
    return getattr(entry, name, None)


class EntryListModel(QAbstractListModel, Generic[EntryT]):
    """把 ``Sequence[EntryT]`` 暴露成 Qt 列表模型的通用基类。

    变更通知全部走 Qt 的 ``begin*/end*`` 接口，绑定的 view 会自动同步；
    内容未变的写入（例如同一份数据重复装载）不发信号，避免视图无谓重绘。
    """

    #: role → 条目字段名。键写 ``ItemRole.Pixmap`` 这样的枚举或裸 int 都行。
    ROLE_FIELDS: ClassVar[Mapping[int, str]] = {}
    #: ``DisplayRole`` 取哪个字段（``None`` 表示不提供默认文本）。
    DISPLAY_FIELD: ClassVar[str | None] = None
    #: ``ToolTipRole`` 取哪个字段（``None`` 时退化为 ``DISPLAY_FIELD``）。
    TOOLTIP_FIELD: ClassVar[str | None] = None
    #: ``DecorationRole`` 取哪个字段（通常是缩略图）。
    DECORATION_FIELD: ClassVar[str | None] = None

    def __init__(
            self,
            entries: Iterable[EntryT] | None = None,
            parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._entries: list[EntryT] = list(entries) if entries else []

    # ------------------------------------------------------------------ #
    # QAbstractListModel
    # ------------------------------------------------------------------ #
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(self, index: QModelIndex, role: int = _DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self.entry_at(index.row())
        if entry is None:
            return None

        role = int(role)
        if role == _DisplayRole:
            return attr_of(entry, self.DISPLAY_FIELD)
        if role == _ToolTipRole:
            return attr_of(entry, self.TOOLTIP_FIELD or self.DISPLAY_FIELD)
        if role == _DecorationRole:
            return attr_of(entry, self.DECORATION_FIELD)

        field = self.ROLE_FIELDS.get(role)
        if field is None:
            return None
        return attr_of(entry, field)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )

    # ------------------------------------------------------------------ #
    # 只读查询
    # ------------------------------------------------------------------ #
    def entries(self) -> list[EntryT]:
        """当前全部条目（浅拷贝，改它不影响模型）。"""
        return list(self._entries)

    def entry_at(self, row: int) -> EntryT | None:
        """取第 ``row`` 行条目；越界返回 ``None``。"""
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def is_empty(self) -> bool:
        return not self._entries

    # ------------------------------------------------------------------ #
    # 整体装载
    # ------------------------------------------------------------------ #
    def set_entries(
            self,
            entries: Iterable[EntryT],
            *,
            skip_if_equal: bool = True,
    ) -> None:
        """整体替换条目（模型 reset）。

        :param skip_if_equal: 与现有内容完全相同时跳过，不发 ``modelReset``
            —— 语言切换 / 定时刷新这类「重复喂同一份数据」的场景靠它免重绘。
        """
        new = list(entries)
        if skip_if_equal and self._same_entries(new):
            return
        self.beginResetModel()
        self._entries = new
        self.endResetModel()

    def clear(self) -> None:
        """清空条目；已经为空时为空操作。"""
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self.endResetModel()

    # ------------------------------------------------------------------ #
    # 增量修改
    # ------------------------------------------------------------------ #
    def append(self, entry: EntryT) -> int:
        """追加一条，返回其行号。"""
        row = len(self._entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.append(entry)
        self.endInsertRows()
        return row

    def append_many(self, entries: Iterable[EntryT]) -> int:
        """批量追加，返回追加后的总行数。"""
        new = list(entries)
        if not new:
            return len(self._entries)
        start = len(self._entries)
        self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
        self._entries.extend(new)
        self.endInsertRows()
        return len(self._entries)

    def insert_entries(self, row: int, entries: Iterable[EntryT]) -> bool:
        """在 ``row`` 处插入若干条；越界时按追加处理。"""
        new = list(entries)
        if not new:
            return False
        row = max(0, min(int(row), len(self._entries)))
        self.beginInsertRows(QModelIndex(), row, row + len(new) - 1)
        self._entries[row:row] = new
        self.endInsertRows()
        return True

    def remove_rows(self, row: int, count: int = 1) -> bool:
        """删除 ``count`` 行；范围非法时返回 ``False``。"""
        count = int(count)
        if count <= 0 or row < 0 or row + count > len(self._entries):
            return False
        self.beginRemoveRows(QModelIndex(), row, row + count - 1)
        del self._entries[row:row + count]
        self.endRemoveRows()
        return True

    def update_entry(self, row: int, entry: EntryT) -> bool:
        """替换第 ``row`` 行；内容未变时返回 ``False`` 且不发信号。"""
        if not 0 <= row < len(self._entries):
            return False
        if self._entries[row] == entry:
            return False
        self._entries[row] = entry
        self.touch_row(row)
        return True

    def patch_entry(self, row: int, **changes: Any) -> bool:
        """就地改条目字段（dataclass / 普通对象），并发 ``dataChanged``。"""
        entry = self.entry_at(row)
        if entry is None:
            return False
        if isinstance(entry, dict):
            entry.update(changes)
        else:
            for name, value in changes.items():
                setattr(entry, name, value)
        self.touch_row(row)
        return True

    def touch_row(self, row: int, roles: Iterable[int] | None = None) -> None:
        """声明第 ``row`` 行变了；``roles`` 为空表示整行都变。"""
        index = self.index(int(row), 0)
        if not index.isValid():
            return
        self.dataChanged.emit(index, index, [int(r) for r in roles] if roles else [])

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _same_entries(self, new: list[EntryT]) -> bool:
        if len(new) != len(self._entries):
            return False
        try:
            return all(old == fresh for old, fresh in zip(self._entries, new))
        except Exception:  # noqa: BLE001 —— 条目自定义 __eq__ 抛错时按「变了」处理
            return False
