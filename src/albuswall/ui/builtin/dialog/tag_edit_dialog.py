#
""""""

from __future__ import annotations

from typing import Literal, Optional

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout,
)

from ..i18n import Retranslatable


class TagEditDialog(Retranslatable, QDialog):
    """新建 / 重命名标签的输入对话框。

    文案（窗口标题、字段提示）统一在 :meth:`retranslate_ui` 里应用：
    构造期与语言切换走同一条路径，避免同一句话在两处各写一份。
    """

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
        self._mode: TagEditDialog.Mode = mode
        #: 调用方显式给的标题（属于数据，不参与翻译）
        self._explicit_title: Optional[str] = title

        layout = QVBoxLayout(self)
        self.prompt_label = QLabel("", self)
        layout.addWidget(self.prompt_label)
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

        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """应用窗口标题与字段提示。"""
        if self._explicit_title:
            self.setWindowTitle(self._explicit_title)
        else:
            self.setWindowTitle(
                self.tr("新建标签")
                if self._mode == "create"
                else self.tr("重命名标签")
            )
        self.prompt_label.setText(self.tr("名称"))

    def value(self) -> str:
        return self._edit.text().strip()
