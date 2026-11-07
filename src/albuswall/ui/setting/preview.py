"""``dry_run`` 结果的变更预览表（视图自己的渲染，不弹写回层对话框）。

从 ``view.py`` 拆出来的：它只依赖 ``changes`` 列表，与设置树 / 状态机无关。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import strings
from .contract import format_value

__all__ = ["SettingsPreviewDialog"]


class SettingsPreviewDialog(QDialog):
    """``dry_run`` 的变更表：键 / 旧值 / 新值 / 文件 / 是否变更。"""

    def __init__(
            self,
            changes: Iterable[Mapping[str, Any]],
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.PREVIEW_TITLE)
        self.resize(940, 420)
        self.setObjectName("SettingPreviewDialog")
        layout = QVBoxLayout(self)

        rows = list(changes)
        hint = QLabel(strings.PREVIEW_HINT.format(count=len(rows)), self)
        hint.setObjectName("SettingFooterHint")
        layout.addWidget(hint)

        table = QTableWidget(len(rows), 5, self)
        table.setHorizontalHeaderLabels(list(strings.PREVIEW_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        for row, change in enumerate(rows):
            values = [
                str(change.get("key") or ""),
                format_value(change.get("old")),
                format_value(change.get("new")),
                str(change.get("file") or ""),
                strings.PREVIEW_YES if change.get("changed") else strings.PREVIEW_NO,
            ]
            for column, text in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(text))
        layout.addWidget(table, 1)

        close = QPushButton(strings.PREVIEW_CLOSE, self)
        close.setObjectName("SettingPreviewButton")
        close.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close)
        layout.addLayout(footer)
