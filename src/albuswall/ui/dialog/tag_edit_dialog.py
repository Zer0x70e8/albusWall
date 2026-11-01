#
"""新建 / 重命名标签的输入对话框。

相比旧版：``mode`` 从 ``Literal["create", "rename"]`` 换成
:class:`TagEditMode` 枚举（拼错立刻报错，不再是运行期静默走错分支）；
文案统一在 :meth:`retranslate_ui` 里应用，构造期与语言切换走同一条路径。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Retranslatable

__all__ = ["TagEditDialog", "TagEditMode"]


class TagEditMode(Enum):
    """对话框用途（决定默认标题与提示）。"""

    CREATE = "create"
    RENAME = "rename"


class TagEditDialog(Retranslatable, QDialog):
    """标签名输入对话框。

    :param mode: 新建 / 重命名，见 :class:`TagEditMode`；也接受等价字符串。
    :param initial: 输入框初始文本（重命名时是旧名字）。
    :param title: 显式窗口标题；``None`` 时按 ``mode`` 取默认文案。
        （显式标题属于数据，不参与翻译。）
    """

    def __init__(
            self,
            mode: TagEditMode | str = TagEditMode.CREATE,
            *,
            initial: str = "",
            title: str | None = None,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = _as_mode(mode)
        self._explicit_title = title

        layout = QVBoxLayout(self)

        self.prompt_label = QLabel("", self)
        layout.addWidget(self.prompt_label)

        self._edit = QLineEdit(self)
        self._edit.setText(initial)
        self._edit.selectAll()
        layout.addWidget(self._edit)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._edit.returnPressed.connect(self.accept)

        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """应用窗口标题与字段提示（幂等，只改文本）。"""
        if self._explicit_title:
            self.setWindowTitle(self._explicit_title)
        elif self._mode is TagEditMode.CREATE:
            self.setWindowTitle(self.tr("新建标签"))
        else:
            self.setWindowTitle(self.tr("重命名标签"))
        self.prompt_label.setText(self.tr("名称"))

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def mode(self) -> TagEditMode:
        return self._mode

    def value(self) -> str:
        """输入框内容（已去首尾空白）。"""
        return self._edit.text().strip()

    def set_value(self, value: str) -> None:
        """重设输入框内容并全选。"""
        self._edit.setText(value)
        self._edit.selectAll()

    def set_title(self, title: str | None) -> None:
        """改显式标题并立即生效；``None`` 时回到按 mode 的默认文案。"""
        self._explicit_title = title
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 便捷构造
    # ------------------------------------------------------------------ #
    @classmethod
    def create(cls, *, title: str | None = None,
               parent: QWidget | None = None) -> "TagEditDialog":
        """新建标签对话框。"""
        return cls(TagEditMode.CREATE, title=title, parent=parent)

    @classmethod
    def rename(
            cls,
            initial: str = "",
            *,
            title: str | None = None,
            parent: QWidget | None = None,
    ) -> "TagEditDialog":
        """重命名标签对话框（预填旧名字）。"""
        return cls(
            TagEditMode.RENAME, initial=initial, title=title, parent=parent)


def _as_mode(mode: TagEditMode | str) -> TagEditMode:
    if isinstance(mode, TagEditMode):
        return mode
    return TagEditMode(str(mode).lower())
