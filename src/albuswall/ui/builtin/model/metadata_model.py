#
""""""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from ..roles.metadata_roles import KEY_ROLE, RAW_VALUE_ROLE, VALUE_ROLE

Formatter = Callable[[Any], str]


# --------------------------------------------------------------------------- #
# 格式化器
# --------------------------------------------------------------------------- #
def _fmt_px(value: Any) -> str:
    try:
        return f"{int(value)} px"
    except (TypeError, ValueError):
        return str(value)


def _fmt_size(value: Any) -> str:
    try:
        n = float(int(value))
    except (TypeError, ValueError):
        return str(value)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _fmt_bool(value: Any) -> str:
    return "是" if bool(value) else "否"


# --------------------------------------------------------------------------- #
# 字段规格：决定「展示哪些字段、什么顺序、用什么标签、怎么格式化」
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    formatter: Optional[Formatter] = None


_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("title", "标题"),
    FieldSpec("description", "描述"),
    FieldSpec("width", "宽度", _fmt_px),
    FieldSpec("height", "高度", _fmt_px),
    FieldSpec("file_size", "文件大小", _fmt_size),
    FieldSpec("mime_type", "MIME 类型"),
    FieldSpec("taken_at", "拍摄时间"),
    FieldSpec("created_at", "加入时间"),
    FieldSpec("modified_at", "修改时间"),
    FieldSpec("deleted_at", "删除时间"),
    FieldSpec("is_favorite", "已收藏", _fmt_bool),
    FieldSpec("is_deleted", "已删除", _fmt_bool),
    FieldSpec("uuid", "UUID"),
    FieldSpec("file_path", "文件路径"),
)


class MetadataModel(QAbstractListModel):
    """信息面板的元信息列表模型。

    输入是 ``presenter`` 组装的 ``Mapping[str, Any]``：
        · 已知字段按 ``_FIELD_SPECS`` 的顺序、标签、格式化器展示；
        · 未知字段追加到末尾，用原始 key 当标签；
        · ``None`` / ``""`` 一律跳过。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # (label, display_value, raw_value)
        self._items: list[tuple[str, str, Any]] = []

    # ------------------------------------------------------------------ #
    # QAbstractListModel
    # ------------------------------------------------------------------ #
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._items):
            return None

        label, value, raw = self._items[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return f"{label}: {value}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(value)
        if role == KEY_ROLE:
            return label
        if role == VALUE_ROLE:
            return value
        if role == RAW_VALUE_ROLE:
            return raw
        return None

    # ------------------------------------------------------------------ #
    # 数据入口
    # ------------------------------------------------------------------ #
    def set_metadata(self, metadata: Optional[Mapping[str, Any]]) -> None:
        items = self._build_items(metadata)
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def clear(self) -> None:
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_items(
            metadata: Optional[Mapping[str, Any]],
    ) -> list[tuple[str, str, Any]]:
        if not metadata:
            return []

        items: list[tuple[str, str, Any]] = []
        seen: set[str] = set()

        for spec in _FIELD_SPECS:
            if spec.key not in metadata:
                continue
            seen.add(spec.key)
            raw = metadata[spec.key]
            if raw is None or raw == "":
                continue
            try:
                value = spec.formatter(raw) if spec.formatter else str(raw)
            except Exception:
                value = str(raw)
            if value == "":
                continue
            items.append((spec.label, value, raw))

        # spec 之外的额外字段：按插入顺序兜底追加
        for key, raw in metadata.items():
            if key in seen:
                continue
            if raw is None or raw == "":
                continue
            items.append((str(key), str(raw), raw))

        return items
