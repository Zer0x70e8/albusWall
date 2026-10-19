#
"""TagBar Presenter。

职责：
  · 把当前资产的标签列表灌进 chip model；
  · 处理新建 / 打标 / 移除；
  · 处理 layer tree 的打开、选择、绑定。

不碰 SQL、不碰业务规则——所有规则都在 TagService / AssetTagService 里。
"""

from __future__ import annotations

from logging import getLogger
from typing import Callable

from PySide6.QtCore import QObject, Signal

from albuswall.ui.builtin.model.tag_chip_model import TagChipModel
from albuswall.ui.builtin.model.tag_tree_model import TagTreeModel
from albuswall.services.asset_tag import AssetTagError, AssetTagService
from albuswall.services.tag import TagError, TagNameConflict, TagService

if __import__("typing").TYPE_CHECKING:
    from albuswall.ui.builtin.window.tagbar import TagBar

_logger = getLogger(__name__)


class TagBarPresenter(QObject):
    tags_changed = Signal()  # 供外层（grid / tree）刷新
    tag_activated = Signal(str)  # chip 点击 → 打开筛选
    error_occurred = Signal(str)  # 供 main window 显示 toast

    def __init__(
            self,
            view: "TagBar",
            tag_service: TagService,
            asset_tag_service: AssetTagService,
            current_asset_provider: Callable[[], str | None],
            parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._view = view
        self._tag = tag_service
        self._asset_tag = asset_tag_service
        self._current_asset = current_asset_provider

        self._chip_model = TagChipModel(self)
        self._tree_model = TagTreeModel(self)
        view.set_chip_model(self._chip_model)
        view.set_tree_model(self._tree_model)

        self._connect()

    # ---------------- 绑定 ----------------
    def _connect(self) -> None:
        v = self._view
        v.apply_requested.connect(self._on_apply_requested)
        v.chip_activated.connect(self._on_chip_activated)
        v.chip_remove_requested.connect(self._on_chip_remove_requested)
        v.layer_tree_open_requested.connect(self.refresh_tree)
        v.layer_tree_activated.connect(self._on_tree_activated)

    # ---------------- 刷新 ----------------
    def refresh(self) -> None:
        """刷新当前资产的 chip 列表。"""
        asset_uuid = self._current_asset()
        if asset_uuid is None:
            self._chip_model.set_tags([])
            return
        try:
            tags = self._asset_tag.list_of_asset(asset_uuid)
        except Exception:
            _logger.exception("list_of_asset failed: uuid=%s", asset_uuid)
            self._chip_model.set_tags([])
            return
        self._chip_model.set_tags(tags)

    def refresh_tree(self) -> None:
        """刷新 layer tree（每次弹出前重建，避免脏数据）。"""
        try:
            roots = self._tag.list_tree()
        except Exception:
            _logger.exception("list_tree failed")
            return
        self._tree_model.set_roots(roots)
        self._view.layer_tree_popup.tree_view.expandAll()

    # ---------------- 槽 ----------------
    def _on_apply_requested(self, text: str) -> None:
        asset_uuid = self._current_asset()
        if asset_uuid is None:
            return
        try:
            new_uuid = self._tag.create(text)
            result = self._asset_tag.add(asset_uuid, [new_uuid])
        except TagNameConflict as e:
            if e.existing_uuid is None:
                self.error_occurred.emit(f"标签已存在：{text}")
                return
            # 存在就降级为「绑定到已有标签」
            try:
                result = self._asset_tag.add(asset_uuid, [e.existing_uuid])
            except AssetTagError as ae:
                self.error_occurred.emit(str(ae))
                return
        except (TagError, AssetTagError) as e:
            self.error_occurred.emit(str(e))
            return

        self._view.clear_apply_input()
        self.refresh()
        if result.added > 0:
            self.tags_changed.emit()

    def _on_chip_activated(self, uuid: str) -> None:
        self.tag_activated.emit(uuid)

    def _on_chip_remove_requested(self, uuid: str) -> None:
        asset_uuid = self._current_asset()
        if asset_uuid is None:
            return
        try:
            self._asset_tag.remove(asset_uuid, [uuid])
        except AssetTagError as e:
            self.error_occurred.emit(str(e))
            return
        self.refresh()
        self.tags_changed.emit()

    def _on_tree_activated(self, uuid: str) -> None:
        asset_uuid = self._current_asset()
        if asset_uuid is None:
            return
        try:
            result = self._asset_tag.add(asset_uuid, [uuid])
        except AssetTagError as e:
            self.error_occurred.emit(str(e))
            return
        if result.added > 0:
            self.refresh()
            self.tags_changed.emit()

    def setup(self, container) -> None:
        pass

    def teardown(self) -> None:
        pairs = (
            (self._view.apply_requested, self._on_apply_requested),
            (self._view.chip_activated, self._on_chip_activated),
            (self._view.chip_remove_requested, self._on_chip_remove_requested),
            (self._view.layer_tree_open_requested, self.refresh_tree),
            (self._view.layer_tree_activated, self._on_tree_activated),
        )
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
