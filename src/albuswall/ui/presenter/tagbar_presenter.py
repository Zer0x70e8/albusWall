#
"""标签栏 presenter：当前资产的 chip 列表 + 标签树。

职责
====

* 把「当前资产」（由主 presenter 跟随光标 / 详情页切换设置）的标签灌进
  chip 模型；
* 处理打标（``父/子`` 路径写法 → ``TagService.ensure_path``）、移除、树内点击；
* 标签**结构**编辑（新建 / 重命名 / 删除）不在这里做对话框，只把意图转发给
  :class:`~albuswall.ui.presenter.tag_editor_presenter.TagEditorPresenter`。

边界：不碰 SQL、不拼路径、不判断重名 —— 规则全在 ``TagService`` /
``AssetTagService`` 里。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal

from albuswall.service.asset import AssetTagError
from albuswall.service.tag import TagError
from albuswall.ui.model import TagChipModel, TagTreeModel

from .base import Presenter, register_presenter

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.tagbar import TagBar
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["TagBarPresenter"]

logger = logging.getLogger(__package__)


class TagBarPresenter(Presenter):
    """标签栏 presenter。"""

    name = "tagbar"

    #: 用户点了某个 chip（载荷 tag_uuid），用于「按标签过滤」。
    tag_activated = Signal(str)
    #: 当前资产的标签集合变化（打标 / 移除成功）。
    tags_changed = Signal()
    #: 面向用户的错误（窗口 toast）。
    error = Signal(str)
    #: 标签结构编辑意图（真正的对话框在 TagEditorPresenter）。
    tag_create_requested = Signal(str)
    tag_rename_requested = Signal(str)
    tag_delete_requested = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._view: TagBar = window.detail.tag_bar
        self._tag_service = self.service("tag_service")
        self._asset_tag_service = self.service("asset_tag_service")

        self._chip_model = TagChipModel(parent=self)
        self._tree_model = TagTreeModel(parent=self)
        self._current_asset_uuid: str | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        view = self._view
        view.set_chip_model(self._chip_model)
        view.set_tree_model(self._tree_model)

        view.apply_requested.connect(self._on_apply_requested)
        view.chip_activated.connect(self.tag_activated.emit)
        view.chip_remove_requested.connect(self._on_chip_remove_requested)
        view.layer_tree_open_requested.connect(self.refresh_tree)
        view.layer_tree_activated.connect(self._on_tree_activated)
        view.tag_create_requested.connect(self.tag_create_requested.emit)
        view.tag_rename_requested.connect(self.tag_rename_requested.emit)
        view.tag_delete_requested.connect(self.tag_delete_requested.emit)

    def retranslate(self) -> None:
        """语言切换：chip / 树的显示名来自数据，重刷一次即可。"""
        self.refresh()
        self.refresh_tree()

    # ------------------------------------------------------------------ #
    # 当前资产
    # ------------------------------------------------------------------ #
    @property
    def current_asset_uuid(self) -> str | None:
        return self._current_asset_uuid

    def set_current_asset(self, asset_uuid: str | None) -> None:
        """切换标签作用对象（空串 / ``None`` = 没有选中资产）。"""
        target = str(asset_uuid).strip() if asset_uuid else None
        target = target or None
        if target == self._current_asset_uuid:
            return
        self._current_asset_uuid = target
        self.refresh()

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """刷新当前资产的 chip 列表。"""
        if self._current_asset_uuid is None or self._asset_tag_service is None:
            self._chip_model.set_tags([])
            return
        try:
            tags = self._asset_tag_service.list_of_asset(
                self._current_asset_uuid)
        except Exception:  # noqa: BLE001 - 读失败按「没有标签」显示
            logger.exception("tagbar presenter: list_of_asset failed")
            tags = []
        self._chip_model.set_tags(tags)

    def refresh_tree(self) -> None:
        """重建标签树（每次弹出前刷新，避免显示脏数据）。"""
        if self._tag_service is None:
            return
        try:
            roots = self._tag_service.list_tree()
        except Exception:  # noqa: BLE001
            logger.exception("tagbar presenter: list_tree failed")
            return
        self._tree_model.set_roots(roots)
        self._view.layer_tree_popup.tree_view.expandAll()

    # ------------------------------------------------------------------ #
    # 槽：打标 / 移除
    # ------------------------------------------------------------------ #
    def _on_apply_requested(self, text: str) -> None:
        """按 ``父/子`` 路径创建（或复用）标签并绑定到当前资产。"""
        if self._tag_service is None or self._asset_tag_service is None:
            return
        if self._current_asset_uuid is None:
            return
        try:
            tag_uuid = self._tag_service.ensure_path(text)
        except TagError as exc:
            self.error.emit(str(exc))
            return
        self._bind_tag(tag_uuid)

    def _on_tree_activated(self, tag_uuid: str) -> None:
        """树里点了某个标签：直接绑到当前资产。"""
        if self._asset_tag_service is None or self._current_asset_uuid is None:
            return
        self._bind_tag(tag_uuid)

    def _bind_tag(self, tag_uuid: Any) -> None:
        if self._asset_tag_service is None or self._current_asset_uuid is None:
            return
        try:
            result = self._asset_tag_service.add(
                self._current_asset_uuid, [tag_uuid])
        except AssetTagError as exc:
            self.error.emit(str(exc))
            return
        self._view.clear_apply_input()
        self.refresh()
        if getattr(result, "added", 0) > 0:
            self.tags_changed.emit()

    def _on_chip_remove_requested(self, tag_uuid: str) -> None:
        if self._asset_tag_service is None or self._current_asset_uuid is None:
            return
        try:
            self._asset_tag_service.remove(
                self._current_asset_uuid, [tag_uuid])
        except AssetTagError as exc:
            self.error.emit(str(exc))
            return
        self.refresh()
        self.tags_changed.emit()


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_tagbar_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("tagbar presenter skipped: no window bound")
        return None
    return TagBarPresenter(container, window=window, parent=main)
