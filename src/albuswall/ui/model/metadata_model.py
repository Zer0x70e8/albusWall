#
"""信息面板的键值行模型。

和旧版相比有三处改动，都是为了「模型直接能用」：

1. **既能吃 ``Mapping``，也能吃对象**：``set_metadata({"width": 1920})`` 与
   ``set_metadata(asset)``（DTO / dataclass / 带同名属性的对象）等价 ——
   面板不再要求 presenter 先拼一个 dict；
2. **字段规格可注入**：:class:`FieldSpec` 决定「展示哪些字段、什么顺序、
   用什么标签、怎么格式化」，外部想加字段只传一份 spec 序列；
3. **本地化走 ``self.tr``**：模型本身是 ``QObject``，语言切换后
   :meth:`MetadataModel.retranslate` 重建标签即可，不需要外部 tr 垫片。

未知字段（spec 之外的 key）按插入顺序兜底追加，用原始 key 当标签；
``None`` / ``""`` 一律跳过。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Callable

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from .base import attr_of
from .roles import MetadataRole

__all__ = ["FieldSpec", "MetadataModel", "format_px", "format_size", "format_bool"]

Formatter = Callable[[Any], str]

_DisplayRole = int(Qt.ItemDataRole.DisplayRole)
_ToolTipRole = int(Qt.ItemDataRole.ToolTipRole)


# --------------------------------------------------------------------------- #
# 通用格式化器
# --------------------------------------------------------------------------- #
def format_px(value: Any) -> str:
    """像素尺寸：``1920`` → ``"1920 px"``。"""
    try:
        return f"{int(value)} px"
    except (TypeError, ValueError):
        return str(value)


def format_size(value: Any) -> str:
    """字节数 → 人类可读（``1536`` → ``"1.5 KB"``）。"""
    try:
        number = float(int(value))
    except (TypeError, ValueError):
        return str(value)
    if number < 1024:
        return f"{int(number)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        number /= 1024.0
        if number < 1024.0:
            return f"{number:.1f} {unit}"
    return f"{number:.1f} PB"


def format_bool(value: Any) -> str:
    """布尔值 → ``"True"`` / ``"False"``（需要本地化时用模型的 ``tr_bool``）。"""
    return "True" if bool(value) else "False"


# --------------------------------------------------------------------------- #
# 字段规格
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个展示字段的规格。

    Attributes:
        key:       取值用的字段名（对象属性名或 mapping 键）。
        label:     源文标签（展示前会再过一次 ``tr``）。
        formatter: 值格式化器；``None`` 时用 ``str()``。
    """

    key: str
    label: str
    formatter: Formatter | None = None


#: 默认字段规格：资产信息面板的常规字段，顺序即展示顺序。
DEFAULT_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("title", "标题"),
    FieldSpec("description", "描述"),
    FieldSpec("width", "宽度", format_px),
    FieldSpec("height", "高度", format_px),
    FieldSpec("file_size", "文件大小", format_size),
    FieldSpec("mime_type", "MIME 类型"),
    FieldSpec("taken_at", "拍摄时间"),
    FieldSpec("created_at", "加入时间"),
    FieldSpec("modified_at", "修改时间"),
    FieldSpec("deleted_at", "删除时间"),
    FieldSpec("is_favorite", "已收藏"),
    FieldSpec("is_deleted", "已删除"),
    FieldSpec("uuid", "UUID"),
    FieldSpec("file_path", "文件路径"),
)


class MetadataModel(QAbstractListModel):
    """信息面板的键值行模型。

    Roles::

        DisplayRole            → ``"标签: 值"``（给默认委托兜底用）
        ToolTipRole            → 显示值的完整文本
        MetadataRole.Key       → 字段显示名（已本地化）
        MetadataRole.Value     → 字段显示值（已格式化）
        MetadataRole.RawValue  → 字段原始值
    """

    def __init__(
            self,
            metadata: Mapping[str, Any] | object | None = None,
            *,
            specs: Sequence[FieldSpec] | None = None,
            parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._specs: tuple[FieldSpec, ...] = tuple(
            specs if specs is not None else DEFAULT_FIELD_SPECS)
        #: 最近一次的数据快照：语言切换时据此重建（标签需要重新翻译）
        self._source: Any = None
        #: ``(label, display_value, raw_value)`` 三元组
        self._rows: list[tuple[str, str, Any]] = []
        if metadata is not None:
            self.set_metadata(metadata)

    # ------------------------------------------------------------------ #
    # QAbstractListModel
    # ------------------------------------------------------------------ #
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = _DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._rows):
            return None

        label, value, raw = self._rows[row]
        role = int(role)
        if role == _DisplayRole:
            return f"{label}: {value}"
        if role == _ToolTipRole:
            return value
        if role == int(MetadataRole.Key):
            return label
        if role == int(MetadataRole.Value):
            return value
        if role == int(MetadataRole.RawValue):
            return raw
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------ #
    # 数据入口
    # ------------------------------------------------------------------ #
    def set_metadata(self, metadata: Mapping[str, Any] | object | None) -> None:
        """装载元信息；``Mapping``、dataclass、DTO 都可以直接传。"""
        self._source = metadata
        self._reset(self._build_rows(metadata))

    def set_object(self, obj: object | None) -> None:
        """:meth:`set_metadata` 的别名（强调入参是对象而不是 dict）。"""
        self.set_metadata(obj)

    def metadata(self) -> Any:
        """最近一次装载的原始入参（未做归一化）。"""
        return self._source

    def retranslate(self) -> None:
        """语言切换后重建标签（数据不变，只重取文案）。"""
        if self._source is None:
            return
        self._reset(self._build_rows(self._source))

    def clear(self) -> None:
        """清空键值区。"""
        self._source = None
        if self._rows:
            self._reset([])

    # 兼容旧名字
    clear_metadata = clear

    # ------------------------------------------------------------------ #
    # 行访问
    # ------------------------------------------------------------------ #
    def row_at(self, row: int) -> tuple[str, str, Any] | None:
        """返回 ``(标签, 显示值, 原始值)``；越界返回 ``None``。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def as_dict(self) -> dict[str, Any]:
        """当前行的 ``{标签: 显示值}`` 快照（导出 / 调试用）。"""
        return {label: value for label, value, _ in self._rows}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def tr_bool(self, value: Any) -> str:
        """布尔值 → 本地化的「是 / 否」。"""
        return self.tr("是") if bool(value) else self.tr("否")

    def _reset(self, rows: list[tuple[str, str, Any]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def _build_rows(self, metadata: Any) -> list[tuple[str, str, Any]]:
        if metadata is None:
            return []

        mapping = _as_mapping(metadata, self._specs)
        if not mapping:
            return []

        rows: list[tuple[str, str, Any]] = []
        seen: set[str] = set()

        for spec in self._specs:
            if spec.key not in mapping:
                continue
            seen.add(spec.key)
            raw = mapping[spec.key]
            if _is_blank(raw):
                continue
            value = _format(spec, raw, self.tr_bool)
            if value == "":
                continue
            rows.append((self.tr(spec.label), value, raw))

        # spec 之外的额外字段：按插入顺序兜底追加
        for key, raw in mapping.items():
            if key in seen or _is_blank(raw):
                continue
            rows.append((str(key), str(raw), raw))

        return rows


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == []


def _format(spec: FieldSpec, raw: Any, tr_bool: Callable[[Any], str]) -> str:
    """按规格格式化；格式化器抛错时退回 ``str(raw)``，不让一格数据毁掉整表。"""
    formatter = spec.formatter
    if formatter is None and spec.key.startswith(("is_", "has_")):
        formatter = tr_bool
    try:
        return formatter(raw) if formatter is not None else str(raw)
    except Exception:  # noqa: BLE001
        return str(raw)


def _as_mapping(
        metadata: Any,
        specs: Sequence[FieldSpec],
) -> dict[str, Any]:
    """把入参归一化为 ``{字段名: 值}``；不认识的对象返回空 dict。"""
    if isinstance(metadata, Mapping):
        return dict(metadata)
    if is_dataclass(metadata) and not isinstance(metadata, type):
        return {
            field.name: getattr(metadata, field.name)
            for field in fields(metadata)
        }
    if hasattr(metadata, "__dict__"):
        return {
            key: value
            for key, value in vars(metadata).items()
            if not key.startswith("_")
        }
    # 退回「按 spec 逐个探测属性」：不是 dataclass 但有 __slots__ 的对象
    probed = {
        spec.key: attr_of(metadata, spec.key)
        for spec in specs
    }
    return {key: value for key, value in probed.items() if value is not None}
