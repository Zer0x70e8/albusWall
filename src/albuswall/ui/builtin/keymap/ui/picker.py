"""动作选择器：``QTreeView`` + 从 ``COMPONENTS`` 一次性构造的静态 model。"""
from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..components import iter_components
from ..model import Action
from .format import breadcrumb, signature_text

__all__ = ["ActionPickerDialog"]


def build_picker_model(actions: Iterable[Action]) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Action", "Component", "Parameters"])
    items: dict[str, QStandardItem] = {}
    for component in iter_components():
        item = QStandardItem(component.label)
        item.setEditable(False)
        item.setSelectable(False)
        item.setToolTip(f"{component.id}\n{component.description}")
        parent = items.get(component.parent or "")
        (parent.appendRow(item) if parent is not None else model.appendRow(item))
        items[component.id] = item
    for action in actions:
        parent = items.get(action.component) or items.get("app")
        if parent is None:
            continue
        item = QStandardItem(action.label)
        item.setEditable(False)
        item.setData(action.id, Qt.ItemDataRole.UserRole)
        item.setToolTip("\n".join(filter(None, [
            action.id, action.description, signature_text(action),
        ])))
        row = [item, QStandardItem(breadcrumb(action.component)),
               QStandardItem(signature_text(action))]
        for extra in row[1:]:
            extra.setEditable(False)
        parent.appendRow(row)
    return model


class ActionPickerDialog(QDialog):
    """按组件树浏览并选择一个动作。"""

    def __init__(
            self, actions: Iterable[Action], parent: QWidget | None = None,
            *, current: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose an action")
        self.resize(760, 520)
        self._model = build_picker_model(actions)
        self._selected = ""

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Actions are grouped by the UI component they belong to."))
        self.tree = QTreeView(self)
        self.tree.setModel(self._model)
        self.tree.setUniformRowHeights(True)
        self.tree.expandAll()
        layout.addWidget(self.tree, 1)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.tree.selectionModel().currentChanged.connect(self._on_current)
        self.tree.doubleClicked.connect(lambda _i: self._accept_if_action())
        if current:
            self._select(current)

    def _on_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        action_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self._selected = action_id
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(action_id))
        if action_id:
            entity = self._model.itemFromIndex(current)
            self.preview.setText(entity.toolTip() if entity is not None else action_id)
        else:
            self.preview.setText("")

    def _accept_if_action(self) -> None:
        if self._selected:
            self.accept()

    def _select(self, action_id: str) -> None:
        for row in range(self._model.rowCount()):
            component = self._model.item(row)
            for child in range(component.rowCount()):
                item = component.child(child, 0)
                if str(item.data(Qt.ItemDataRole.UserRole) or "") == action_id:
                    self.tree.setCurrentIndex(item.index())
                    return

    @classmethod
    def pick(
            cls, actions: Iterable[Action], parent: QWidget | None = None,
            *, current: str = "",
    ) -> str | None:
        dialog = cls(actions, parent, current=current)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog._selected or None
        finally:
            dialog.deleteLater()
