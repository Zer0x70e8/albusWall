"""薄 Qt Model：snapshot → 两级树 + 搜索代理；``_notify`` 触发 reset。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)

from ..model import Binding
from ..viewmodel import KeymapViewModel, KeyRow, OperationRow
from .format import args_text, breadcrumb, key_text

__all__ = ["BindingModel", "BindingProxy"]

_CHILD = 1 << 20
_ROOT = QModelIndex()


class BindingModel(QAbstractItemModel):
    """按键（父行）→ 操作（子行）两级树。"""

    KeyRole = Qt.ItemDataRole.UserRole + 1
    OperationRole = Qt.ItemDataRole.UserRole + 2
    CustomizedRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, viewmodel: KeymapViewModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._rows: tuple[KeyRow, ...] = ()
        self._reset_pending = False
        self._defer_reset = False
        self._unsubscribe = viewmodel.subscribe(self._schedule_reset)
        self._reset()

    def shutdown(self) -> None:
        self._unsubscribe()

    def _schedule_reset(self) -> None:
        # setData 期间不重建模型（delegate 还在提交），下一个事件循环再刷新
        if not self._defer_reset:
            self._reset()
            return
        if self._reset_pending:
            return
        self._reset_pending = True
        QTimer.singleShot(0, lambda: self._reset_pending and self._reset())

    def _reset(self) -> None:
        self._reset_pending = False
        self.beginResetModel()
        self._rows = self._viewmodel.snapshot()
        self.endResetModel()

    def _key_row(self, index: QModelIndex) -> KeyRow | None:
        row = index.internalId() & (_CHILD - 1)
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = _ROOT) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._rows)
        if parent.internalId() & _CHILD:
            return 0
        key_row = self._key_row(parent)
        return len(key_row.operations) if key_row is not None else 0

    def columnCount(self, parent: QModelIndex = _ROOT) -> int:  # noqa: N802
        return 2

    def index(self, row: int, column: int, parent: QModelIndex = _ROOT) -> QModelIndex:
        if row < 0 or column < 0 or column >= 2:
            return QModelIndex()
        if parent.isValid():
            if parent.internalId() & _CHILD:
                return QModelIndex()
            key_row = self._key_row(parent)
            if key_row is None or row >= len(key_row.operations):
                return QModelIndex()
            return self.createIndex(
                row, column, (parent.internalId() & (_CHILD - 1)) | _CHILD)
        if row >= len(self._rows):
            return QModelIndex()
        return self.createIndex(row, column, row)

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not index.isValid() or not (index.internalId() & _CHILD):
            return QModelIndex()
        row = index.internalId() & (_CHILD - 1)
        return self.createIndex(row, 0, row)

    def hasChildren(self, parent: QModelIndex = _ROOT) -> bool:  # noqa: N802
        return self.rowCount(parent) > 0

    def key_row(self, index: QModelIndex) -> KeyRow | None:
        return self._key_row(index) if index.isValid() else None

    def operation_row(self, index: QModelIndex) -> OperationRow | None:
        if not index.isValid() or not (index.internalId() & _CHILD):
            return None
        key_row = self._key_row(index)
        if key_row is None or not 0 <= index.row() < len(key_row.operations):
            return None
        return key_row.operations[index.row()]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key_row = self._key_row(index)
        if key_row is None:
            return None
        child = bool(index.internalId() & _CHILD)
        if role == self.KeyRole:
            return key_row.key
        if role == self.CustomizedRole:
            return key_row.customized

        if child:
            operation = self.operation_row(index)
            if operation is None:
                return None
            action = operation.action
            if role == self.OperationRole:
                return operation
            if role == Qt.ItemDataRole.DisplayRole:
                if index.column() == 0:
                    return action.label
                details = [breadcrumb(action.component)]
                if operation.args:
                    details.append(args_text(Binding(operation.action_id, operation.args)))
                return " · ".join(d for d in details if d)
            if role == Qt.ItemDataRole.ToolTipRole:
                return "\n".join(filter(None, [
                    action.label, action.id, action.description,
                    breadcrumb(action.component),
                ]))
            if role == Qt.ItemDataRole.FontRole:
                return None
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return key_text(key_row.key)
            count = len(key_row.operations)
            return f"{count} operation{'s' if count != 1 else ''}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(op.action.label for op in key_row.operations)
        if role == Qt.ItemDataRole.EditRole and index.column() == 0:
            return key_row.key
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not (index.internalId() & _CHILD) and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(  # noqa: N802
            self, index: QModelIndex, value: Any,
            role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        key_row = self._key_row(index)
        if key_row is None or (index.internalId() & _CHILD):
            return False
        old, new = key_row.key, str(value or "").strip()
        if not new or old == new:
            return False
        self._defer_reset = True
        try:
            self._viewmodel.rebind_key(old, new)
        except ValueError:
            return False
        finally:
            self._defer_reset = False
        return True

    def headerData(  # noqa: N802
            self, section: int, orientation: Qt.Orientation,
            role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return "Key" if section == 0 else "Operations"


class BindingProxy(QSortFilterProxyModel):
    """搜索代理：按键 token + 每个子操作的 token 都参与匹配。"""

    def filterAcceptsRow(  # noqa: N802
            self, source_row: int, source_parent: QModelIndex,
    ) -> bool:
        regex = self.filterRegularExpression()
        if not regex.pattern():
            return True
        model = self.sourceModel()
        assert isinstance(model, BindingModel)
        if source_parent.isValid():
            key_row = model.key_row(source_parent)
            if key_row is not None and _match(regex, key_row.tokens):
                return True
            operation = model.operation_row(model.index(source_row, 0, source_parent))
            return operation is not None and _match(regex, operation.tokens)
        key_row = model.key_row(model.index(source_row, 0, QModelIndex()))
        if key_row is None:
            return False
        if _match(regex, key_row.tokens):
            return True
        return any(_match(regex, op.tokens) for op in key_row.operations)


def _match(regex: Any, tokens: tuple[str, ...]) -> bool:
    return regex.match(" ".join(tokens)).hasMatch()
