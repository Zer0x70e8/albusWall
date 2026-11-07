#
"""设置页「再处理」的三个小对话框（认领 / 移动 / 添加组件）。

它们只收集字段、返回数据对象；登记与持久化在
:class:`~albuswall.ui.presenter.settings_presenter.SettingsPresenter`。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import strings
from .layout import LayoutComponent, LayoutGroup

__all__ = [
    "AddComponentDialog",
    "MoveItemDialog",
]

_NEW_GROUP = "__new_group__"

class _GroupSelector(QWidget):
    """「已有分组 / 新建分组」二选一的小组件。"""

    def __init__(
            self,
            groups: Sequence[tuple[str, str]],
            *,
            allow_empty: bool = False,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._groups = list(groups)
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.combo = QComboBox(self)
        if allow_empty:
            self.combo.addItem(strings.DIALOG_EMPTY_OPTION, "")
        for key, title in self._groups:
            self.combo.addItem(f"{title}  [{key}]", key)
        self.combo.addItem(strings.DIALOG_NEW_GROUP_OPTION, _NEW_GROUP)
        self.combo.currentIndexChanged.connect(self._sync)

        self.new_key = QLineEdit(self)
        self.new_key.setPlaceholderText(strings.DIALOG_NEW_GROUP_ID_PLACEHOLDER)
        self.new_title = QLineEdit(self)
        self.new_title.setPlaceholderText(strings.DIALOG_NEW_GROUP_TITLE_PLACEHOLDER)
        self.new_description = QLineEdit(self)
        self.new_description.setPlaceholderText(
            strings.DIALOG_NEW_GROUP_DESCRIPTION_PLACEHOLDER,
        )
        self.new_order = QSpinBox(self)
        self.new_order.setRange(0, 9999)
        self.new_order.setValue(900)

        layout.addRow(strings.DIALOG_GROUP_TARGET, self.combo)
        layout.addRow(strings.DIALOG_NEW_GROUP_ID, self.new_key)
        layout.addRow(strings.DIALOG_NEW_GROUP_TITLE, self.new_title)
        layout.addRow(strings.DIALOG_NEW_GROUP_DESCRIPTION, self.new_description)
        layout.addRow(strings.DIALOG_NEW_GROUP_ORDER, self.new_order)
        self._sync()

    def _sync(self) -> None:
        new = self.combo.currentData() == _NEW_GROUP
        for widget in (
            self.new_key, self.new_title, self.new_description, self.new_order,
        ):
            widget.setEnabled(new)

    def values(self) -> tuple[str, str, str, int]:
        """返回 ``(key, title, description, order)``；``key`` 为空表示没选。"""
        data = self.combo.currentData()
        if data == _NEW_GROUP:
            return (
                self.new_key.text().strip(),
                self.new_title.text().strip(),
                self.new_description.text().strip(),
                self.new_order.value(),
            )
        return (str(data or ""), "", "", 0)

    def validate(self) -> str | None:
        key, _title, _description, _order = self.values()
        if not key:
            return strings.DIALOG_REQUIRE_GROUP
        existing = {group_key for group_key, _ in self._groups}
        if key not in existing and self.combo.currentData() == _NEW_GROUP:
            return None
        return None


class MoveItemDialog(QDialog):
    """把一个叶子移动到别的分组（可新建展示分组）。"""

    def __init__(
            self,
            key: str,
            groups: Sequence[tuple[str, str]],
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.DIALOG_MOVE_TITLE)
        self._key = key
        self.resize(520, 360)

        root = QVBoxLayout(self)
        heading = QLabel(strings.DIALOG_MOVE_HEADING.format(key=key), self)
        heading.setTextFormat(Qt.TextFormat.RichText)
        heading.setWordWrap(True)
        root.addWidget(heading)

        form = QFormLayout()
        self._selector = _GroupSelector(groups, parent=self)
        self._order = QSpinBox(self)
        self._order.setRange(0, 9999)
        self._order.setValue(0)
        form.addRow(strings.DIALOG_MOVE_TARGET, self._selector)
        form.addRow(strings.DIALOG_MOVE_ORDER, self._order)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_accept(self) -> None:
        error = self._selector.validate()
        if error:
            QMessageBox.warning(self, strings.DIALOG_MOVE_TITLE, error)
            return
        self.accept()

    def target(self) -> tuple[str, str, str, int, int]:
        """``(group_key, title, description, group_order, item_order)``。"""
        group_key, title, description, group_order = self._selector.values()
        return (
            group_key, title, description, group_order, self._order.value(),
        )


class AddComponentDialog(QDialog):
    """新增一个只含 label 的自定义分组（例如 About）。"""

    def __init__(
            self,
            *,
            key: str = "custom_about",
            title: str = strings.DIALOG_DEFAULT_TITLE,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.DIALOG_ADD_TITLE)
        self.resize(540, 400)

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._key = QLineEdit(key, self)
        self._title = QLineEdit(title, self)
        self._description = QLineEdit("", self)
        self._order = QSpinBox(self)
        self._order.setRange(0, 9999)
        self._order.setValue(900)
        self._label = QPlainTextEdit(self)
        self._label.setPlaceholderText(strings.DIALOG_ADD_LABEL)
        self._label.setPlainText("")
        form.addRow(strings.DIALOG_ADD_KEY, self._key)
        form.addRow(strings.DIALOG_ADD_TITLE_LABEL, self._title)
        form.addRow(strings.DIALOG_ADD_DESCRIPTION, self._description)
        form.addRow(strings.DIALOG_ADD_ORDER, self._order)
        form.addRow("label", self._label)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._key.text().strip():
            QMessageBox.warning(self, strings.DIALOG_ADD_TITLE, strings.DIALOG_REQUIRE_KEY)
            return
        if not self._label.toPlainText().strip():
            QMessageBox.warning(self, strings.DIALOG_ADD_TITLE, strings.DIALOG_REQUIRE_LABEL)
            return
        self.accept()

    def spec(self) -> LayoutGroup:
        return LayoutGroup(
            key=self._key.text().strip(),
            title=self._title.text().strip() or self._key.text().strip(),
            description=self._description.text().strip(),
            order=self._order.value(),
            components=[
                LayoutComponent(text=self._label.toPlainText().strip()),
            ],
        )
