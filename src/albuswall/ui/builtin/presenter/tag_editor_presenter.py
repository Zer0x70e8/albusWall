#
"""标签编辑器 Presenter（rename / delete / restore）。

tree panel 通过 QMenu 触发这些操作，不负责对话框本身。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from albuswall.services.tag import (
    TagError,
    TagHasChildren,
    TagNotFound,
    TagParentNotFound,
    TagService,
)
from albuswall.ui.builtin.dialog.tag_edit_dialog import TagEditDialog


class TagEditorPresenter(QObject):
    changed = Signal()  # 任一写操作完成
    error_occurred = Signal(str)

    def __init__(
            self,
            tag_service: TagService,
            parent_widget=None,
            parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._tag = tag_service
        self._parent = parent_widget

    # ---- 重命名 ----
    def rename(self, tag_uuid: str) -> None:
        try:
            tag = self._tag.get(tag_uuid)
        except TagNotFound as e:
            self.error_occurred.emit(str(e))
            return
        dlg = TagEditDialog(
            "rename", initial=tag.display_name or tag.name, parent=self._parent,
        )
        if dlg.exec() != TagEditDialog.DialogCode.Accepted:
            return
        new_name = dlg.value()
        if not new_name or new_name == tag.name:
            return
        try:
            self._tag.rename(tag_uuid, new_name)
        except (TagError, TagNotFound) as e:
            self.error_occurred.emit(str(e))
            return
        self.changed.emit()

    # ---- 新建 ----
    def create(self, parent_uuid: str | None = None) -> None:
        dlg = TagEditDialog("create", parent=self._parent)
        if dlg.exec() != TagEditDialog.DialogCode.Accepted:
            return
        name = dlg.value()
        if not name:
            return
        try:
            self._tag.create(name, parent_uuid)
        except (TagError, TagParentNotFound) as e:
            self.error_occurred.emit(str(e))
            return
        self.changed.emit()

    # ---- 删除 ----
    def delete(self, tag_uuid: str) -> None:
        try:
            # 先探测是否有子节点（不级联时抛 TagHasChildren）
            self._tag.delete(tag_uuid, cascade=False)
        except TagHasChildren as e:
            ans = QMessageBox.question(
                self._parent,
                self.tr("标签含子节点"),
                self.tr("该标签下有 {count} 个子标签，"
                        "是否连同子标签一起删除？").format(
                    count=len(e.children_uuids)
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                self._tag.delete(tag_uuid, cascade=True)
            except TagError as de:
                self.error_occurred.emit(str(de))
                return
        except TagNotFound as e:
            self.error_occurred.emit(str(e))
            return
        self.changed.emit()

    # ---- 恢复 ----
    def restore(self, tag_uuid: str) -> None:
        try:
            result = self._tag.restore(tag_uuid)
        except TagError as e:
            self.error_occurred.emit(str(e))
            return
        if result.tag is not None and not result.parent_visible:
            QMessageBox.information(
                self._parent,
                self.tr("父标签仍在回收站"),
                self.tr("标签已恢复，但父标签仍在回收站，当前为孤儿状态。"),
            )
        self.changed.emit()

    def setup(self, _=None) -> None:
        """无 container 依赖，保持 manager 协议。"""

    def teardown(self) -> None:
        pass  # 无持久连接，QMessageBox / Dialog 都是一次性的
