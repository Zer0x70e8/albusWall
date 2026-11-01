#
"""标签结构编辑器 presenter：新建 / 重命名 / 删除 / 恢复标签。

标签栏（:class:`~albuswall.ui.presenter.tagbar_presenter.TagBarPresenter`）
只发意图，对话框与写操作都在这里：

* 新建 / 重命名 → :class:`~albuswall.ui.dialog.tag_edit_dialog.TagEditDialog`；
* 删除有子标签时二次确认（``TagHasChildren`` → 询问是否级联）；
* 恢复后父标签仍在回收站时提示「孤儿状态」。

写操作完成后广播 ``changed``，标签栏据此重建树。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from albuswall.service.tag import (
    TagError,
    TagHasChildren,
    TagNotFound,
    TagParentNotFound,
)
from albuswall.ui.dialog import TagEditDialog, TagEditMode

from .base import Presenter, register_presenter

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["TagEditorPresenter"]

logger = logging.getLogger(__package__)


class TagEditorPresenter(Presenter):
    """标签创建 / 重命名 / 删除 / 恢复。"""

    name = "tag_editor"

    #: 任一写操作完成（标签树需要重建）。
    changed = Signal()
    #: 面向用户的错误（窗口 toast）。
    error = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window: QWidget = window
        self._tag_service = self.service("tag_service")

    # ------------------------------------------------------------------ #
    # 写操作
    # ------------------------------------------------------------------ #
    def create(self, parent_uuid: str = "") -> None:
        """新建标签；``parent_uuid`` 为空表示建根标签。"""
        if self._tag_service is None:
            return
        dialog = TagEditDialog(
            TagEditMode.CREATE, parent=self._window)
        if dialog.exec() != TagEditDialog.DialogCode.Accepted:
            return
        name = dialog.value().strip()
        if not name:
            return
        try:
            self._tag_service.create(name, parent_uuid or None)
        except TagParentNotFound:
            self.error.emit(self.tr("父标签不存在"))
            return
        except TagError as exc:
            self.error.emit(str(exc))
            return
        self.changed.emit()

    def rename(self, tag_uuid: str) -> None:
        """重命名标签（初始值取当前显示名）。"""
        if self._tag_service is None:
            return
        try:
            tag = self._tag_service.get(tag_uuid)
        except TagNotFound:
            self.error.emit(self.tr("标签不存在"))
            return
        initial = str(
            getattr(tag, "display_name", "") or getattr(tag, "name", "") or "")
        dialog = TagEditDialog(
            TagEditMode.RENAME, initial=initial, parent=self._window)
        if dialog.exec() != TagEditDialog.DialogCode.Accepted:
            return
        new_name = dialog.value().strip()
        if not new_name or new_name == initial:
            return
        try:
            self._tag_service.rename(tag_uuid, new_name)
        except TagError as exc:
            self.error.emit(str(exc))
            return
        self.changed.emit()

    def delete(self, tag_uuid: str) -> None:
        """删除标签；有子标签时二次确认是否级联。"""
        if self._tag_service is None:
            return
        try:
            self._tag_service.delete(tag_uuid, cascade=False)
        except TagHasChildren as exc:
            answer = QMessageBox.question(
                self._window,
                self.tr("标签含子标签"),
                self.tr("该标签下有 {count} 个子标签，是否连同子标签一起删除？")
                .format(count=len(exc.children_uuids)),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                self._tag_service.delete(tag_uuid, cascade=True)
            except TagError as nested:
                self.error.emit(str(nested))
                return
        except TagNotFound:
            self.error.emit(self.tr("标签不存在"))
            return
        except TagError as exc:
            self.error.emit(str(exc))
            return
        self.changed.emit()

    def restore(self, tag_uuid: str) -> None:
        """从回收站恢复标签；父标签仍不可见时给出提示。"""
        if self._tag_service is None:
            return
        try:
            result = self._tag_service.restore(tag_uuid)
        except TagError as exc:
            self.error.emit(str(exc))
            return
        if result.tag is not None and not result.parent_visible:
            QMessageBox.information(
                self._window,
                self.tr("父标签仍在回收站"),
                self.tr("标签已恢复，但父标签仍在回收站，当前为孤儿状态。"),
            )
        self.changed.emit()


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_tag_editor_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("tag editor presenter skipped: no window bound")
        return None
    return TagEditorPresenter(container, window=window, parent=main)
