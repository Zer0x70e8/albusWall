#
""""""

from __future__ import annotations

from typing import Literal

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout,
)


class TagEditDialog(QDialog):
    Mode = Literal["create", "rename"]

    def __init__(
            self,
            mode: Mode,
            *,
            initial: str = "",
            title: str | None = None,
            parent=None,
    ):
        super().__init__(parent)
        self._mode = mode
        self.setWindowTitle(title or ("新建标签" if mode == "create" else "重命名标签"))

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("名称"))
        self._edit = QLineEdit(self)
        self._edit.setText(initial)
        self._edit.selectAll()
        layout.addWidget(self._edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._edit.returnPressed.connect(self.accept)

    def value(self) -> str:
        return self._edit.text().strip()
