"""快捷键帮助面板（``?`` / ``Ctrl+/``）。

从注册中心**现读**最终键位，所以用户改过绑定后打开看到的就是生效值；
不缓存、不落盘。分组沿用组件树（``keymap.components``）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..components import component_children, component_path
from ..model import registry
from .format import breadcrumb, key_text

__all__ = ["KEYMAP_HELP_TITLE", "KeymapHelpDialog", "show_help"]

KEYMAP_HELP_TITLE = "Keyboard shortcuts"


def _keys_by_action() -> dict[str, list[str]]:
    """``action_id`` → 该动作当前绑定的键（``seq`` 顺序由调用方决定）。"""
    table: dict[str, list[str]] = {}
    for key, bindings in registry.effective_bindings().items():
        for binding in bindings:
            table.setdefault(binding.action, []).append(key)
    return table


def _rank(component_id: str) -> int:
    """组件树的深度优先顺序编号，用于把动作按界面位置排序。"""
    chain = component_path(component_id)
    if not chain:
        return 10**6
    index = 0
    for node in chain:
        siblings = component_children(node.parent)
        for position, child in enumerate(siblings):
            if child.id == node.id:
                index = index * 100 + position
                break
    return index


class KeymapHelpDialog(QDialog):
    """只读的帮助面板：组件 → 动作（键 + 说明）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr(KEYMAP_HELP_TITLE))
        self.resize(680, 640)

        layout = QVBoxLayout(self)
        intro = QLabel(
            self.tr("Shortcuts are read from the current bindings. "
                    "Edit them in Settings → Key bindings."),
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([self.tr("Action"), self.tr("Key")])
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate()

    def _populate(self) -> None:
        keys_by_action = _keys_by_action()
        components: dict[str, QTreeWidgetItem] = {}
        actions = sorted(
            registry.iter_actions(),
            key=lambda action: (_rank(action.component), action.seq),
        )
        for action in actions:
            keys = keys_by_action.get(action.id)
            if not keys:
                continue
            group_id = action.component
            parent = components.get(group_id)
            if parent is None:
                parent = QTreeWidgetItem(
                    self.tree, [breadcrumb(group_id), ""])
                parent.setFirstColumnSpanned(True)
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                components[group_id] = parent
            item = QTreeWidgetItem(parent, [
                f"{action.label}  ·  {action.description}",
                " / ".join(key_text(key) for key in keys),
            ])
            item.setToolTip(0, f"{action.id}\n{action.description}")
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.expandAll()
        for column in range(2):
            self.tree.resizeColumnToContents(column)


def show_help(parent: QWidget | None = None) -> bool:
    """打开帮助面板；返回是否展示成功（无 Qt 环境时返回 False）。"""
    try:
        dialog = KeymapHelpDialog(parent)
    except Exception:  # noqa: BLE001 - 帮助面板失败不影响主界面
        return False
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()
    return True
