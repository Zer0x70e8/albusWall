"""设置页：QTreeView + 搜索代理 + 按键 delegate + 参数对话框。

BindingModel（薄 model）→ BindingProxy（搜索）→ KeymapPage（组装）；
提交后由注册中心 ``_notify`` 驱动重绘，页面不手动重建控件。
"""
from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QModelIndex, QRegularExpression
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..model import registry
from ..viewmodel import KeymapViewModel, OperationRow
from .arguments import build_form, clear_form
from .binding_model import BindingModel, BindingProxy
from .delegate import KeyColumnDelegate
from .format import breadcrumb
from .picker import ActionPickerDialog

__all__ = [
    "PAGE_DESCRIPTION", "PAGE_ID", "PAGE_ORDER", "PAGE_TITLE", "KeymapPage",
    "build_keybindings", "register_keymap_page",
]

PAGE_ID = "keybindings"
PAGE_TITLE = "Key bindings"
PAGE_ORDER = 800
PAGE_DESCRIPTION = (
    "Every key and the operations it triggers. A key runs its operations in "
    "registration order; changes are saved to the [shortcuts] section "
    "immediately."
)


class _OperationDialog(QDialog):
    """编辑一个操作的参数，或把它从键上删掉。"""

    def __init__(self, operation: OperationRow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.remove_requested = False
        self._action = operation.action
        self.setWindowTitle(operation.action.label)
        self.resize(520, 260)
        self._form_layout = QFormLayout()
        self._form = build_form(operation.action, dict(operation.args), self._form_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(operation.action.description))
        layout.addWidget(QLabel(breadcrumb(operation.action.component)))
        layout.addLayout(self._form_layout)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.addButton(
            "Remove", QDialogButtonBox.ButtonRole.DestructiveRole
        ).clicked.connect(self._on_remove)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _on_remove(self) -> None:
        self.remove_requested = True
        self.accept()

    def values(self) -> dict[str, Any]:
        return self._form.values()


class KeymapPage(QWidget):
    """按键映射设置页（构造即接好 model / proxy / delegate）。"""

    def __init__(
            self, parent: QWidget | None = None, *,
            viewmodel: KeymapViewModel | None = None,
    ) -> None:
        super().__init__(parent)
        self.viewmodel = viewmodel or KeymapViewModel(registry)
        self.model = BindingModel(self.viewmodel, self)
        self.proxy = BindingProxy(self)
        self.proxy.setSourceModel(self.model)
        self._action = None
        self._form = None
        self._build_ui()
        self._update_status()
        self._unsubscribe_status = self.viewmodel.subscribe(self._update_status)
        self.destroyed.connect(self._shutdown)

    def _shutdown(self, *_args: Any) -> None:
        self._unsubscribe_status()
        self.model.shutdown()
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 4)
        layout.setSpacing(8)
        intro = QLabel(PAGE_DESCRIPTION, self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.enabled_box = QCheckBox("Enable key bindings", self)
        self.enabled_box.setChecked(self.viewmodel.enabled)
        self.enabled_box.toggled.connect(self.viewmodel.set_enabled)
        layout.addWidget(self.enabled_box)

        toolbar = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Search keys, actions or parameters…")
        self.search.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search, 1)
        self.help_button = QPushButton("Help", self)
        self.help_button.clicked.connect(self._on_help)
        toolbar.addWidget(self.help_button)
        self.reset_button = QPushButton("Reset all", self)
        self.reset_button.clicked.connect(self._on_reset_all)
        toolbar.addWidget(self.reset_button)
        layout.addLayout(toolbar)

        layout.addWidget(self._build_add_form())

        self.tree = QTreeView(self)
        self.tree.setModel(self.proxy)
        self.tree.setItemDelegateForColumn(0, KeyColumnDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _build_add_form(self) -> QWidget:
        box = QWidget(self)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 4, 0, 4)
        row = QHBoxLayout()
        row.addWidget(QLabel("Key", box))
        self.key_editor = QKeySequenceEdit(box)
        self.key_editor.setMaximumSequenceLength(1)
        self.key_editor.setClearButtonEnabled(True)
        self.key_editor.setMinimumWidth(180)
        self.key_editor.setMaximumWidth(260)
        self.key_editor.keySequenceChanged.connect(self._preview_conflicts)
        row.addWidget(self.key_editor)
        self.choose_button = QPushButton("Choose action…", box)
        self.choose_button.clicked.connect(self._choose_action)
        row.addWidget(self.choose_button)
        self.add_button = QPushButton("Add", box)
        self.add_button.clicked.connect(self._on_add)
        row.addWidget(self.add_button)
        row.addStretch(1)
        outer.addLayout(row)

        self.form_host = QWidget(box)
        self.form_layout = QFormLayout(self.form_host)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.form_host)
        self.add_error = QLabel("", box)
        self.add_error.setWordWrap(True)
        self.add_error.hide()
        outer.addWidget(self.add_error)
        self.add_warning = QLabel("", box)
        self.add_warning.setWordWrap(True)
        self.add_warning.hide()
        outer.addWidget(self.add_warning)
        return box

    def _on_search(self, text: str) -> None:
        pattern = re.escape(text.strip())
        self.proxy.setFilterRegularExpression(
            QRegularExpression(pattern, QRegularExpression.PatternOption.CaseInsensitiveOption))
        self.tree.expandAll()

    def _on_reset_all(self) -> None:
        self.viewmodel.reset_all()
        self.tree.expandAll()

    def _on_help(self) -> None:
        """打开只读的帮助面板（与 ``?`` 快捷键同一实现）。"""
        from .help import show_help

        show_help(self)

    def _choose_action(self) -> None:
        action_id = ActionPickerDialog.pick(
            self.viewmodel.iter_actions(), self,
            current=self._action.id if self._action else "")
        if action_id:
            self._set_action(action_id)

    def _set_action(self, action_id: str | None) -> None:
        self._action = self.viewmodel.get_action(action_id) if action_id else None
        if self._action is None:
            self.choose_button.setText("Choose action…")
            self._clear_form()
            return
        self.choose_button.setText(self._action.label)
        self._form = build_form(self._action, None, self.form_layout)

    def _clear_form(self) -> None:
        clear_form(self.form_layout)

    def _on_add(self) -> None:
        action = self._action
        if action is None:
            return self._error("Choose an action first.")
        key = self.key_editor.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText)
        if not key:
            return self._error("Press a key (or key combination) first.")
        try:
            values = self._form.values() if self._form is not None else {}
            self.viewmodel.add_operation(key, action.id, values)
        except ValueError as exc:
            return self._error(str(exc))
        self.add_error.hide()
        self._warn_conflicts(key, action.id)
        self.key_editor.clear()
        self._set_action(None)
        self.tree.expandAll()

    def _warn_conflicts(self, key: str, action_id: str) -> None:
        """同一个键可以挂多个操作；提前告诉用户「它们会一起触发」。"""
        others = self.viewmodel.conflicts(key, action_id=action_id)
        if not others:
            self.add_warning.hide()
            return
        labels = ", ".join(item.label for item in others)
        self.add_warning.setText(
            f"{key} already runs: {labels}. Both operations will run.")
        self.add_warning.show()

    def _preview_conflicts(self, *_args: object) -> None:
        """录键过程中就提示冲突（还没提交绑定）。"""
        action = self._action
        key = self.key_editor.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText)
        if action is None or not key:
            self.add_warning.hide()
            return
        self._warn_conflicts(key, action.id)

    def _error(self, message: str) -> None:
        self.add_error.setText(message)
        self.add_error.show()

    def _on_double_click(self, proxy_index: QModelIndex) -> None:
        index = self.proxy.mapToSource(proxy_index)
        operation = self.model.operation_row(index)
        key_row = self.model.key_row(index)
        if operation is None or key_row is None:
            return
        dialog = _OperationDialog(operation, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                if dialog.remove_requested:
                    self.viewmodel.remove_operation(key_row.key, operation)
                else:
                    self.viewmodel.update_operation(
                        key_row.key, operation, dialog.values())
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid arguments", str(exc))
        finally:
            dialog.deleteLater()

    def _update_status(self) -> None:
        rows = self.viewmodel.snapshot()
        operations = sum(len(row.operations) for row in rows)
        customized = sum(1 for row in rows if row.customized)
        parts = [f"{len(rows)} keys", f"{operations} operations"]
        if customized:
            parts.append(f"{customized} customized")
        if not self.viewmodel.enabled:
            parts.append("disabled")
        if self.search.text().strip():
            parts.append(f"{self.proxy.rowCount()} visible")
        self.status.setText(" · ".join(parts))


def build_keybindings(host: QWidget) -> None:
    """设置页自定义页的构建回调（第一次展开时才跑）。"""
    page = KeymapPage(host)
    layout = host.layout()
    if layout is None:
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)


def register_keymap_page() -> None:
    from albuswall.ui.builtin.setting.pages import PageSpec, register_page

    register_page(
        PageSpec(
            id=PAGE_ID, title=PAGE_TITLE, order=PAGE_ORDER,
            description=PAGE_DESCRIPTION, build=build_keybindings,
        ),
        replace=True,
    )

