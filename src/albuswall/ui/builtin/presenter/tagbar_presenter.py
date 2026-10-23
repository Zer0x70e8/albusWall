#
"""TagBar presenter。

职责：
  · 把当前资产的标签列表灌进 chip model；
  · 处理新建 / 打标 / 移除；
  · 处理 layer tree 的打开、选择、绑定。

不碰 SQL、不碰业务规则——所有规则都在 TagService / AssetTagService 里。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, Signal

from albuswall.log import getLogger
from albuswall.services.asset_tag import AssetTagError, AssetTagService
from albuswall.services.tag import TagError, TagNameConflict, TagService
from albuswall.ui.builtin.model.tag_chip_model import TagChipModel
from albuswall.ui.builtin.model.tag_tree_model import TagTreeModel
from albuswall.ui.builtin.utils.signal_adapter import SignalAdapter

if TYPE_CHECKING:
    from albuswall.ui.builtin.window.tagbar import TagBar

__all__ = ["TagBarPresenter"]

_logger = getLogger(__name__)


class TagBarPresenter(QObject):
    tags_changed = Signal()  # 供外层（grid / tree）刷新
    tag_activated = Signal(str)  # chip 点击 → 打开筛选
    error_occurred = Signal(str)  # 供 main window 显示 toast
    # 标签结构编辑意图（真正的对话框 / 规则在 TagEditorPresenter）
    tag_create_requested = Signal(str)  # 父标签 uuid；空串 = 根
    tag_rename_requested = Signal(str)
    tag_delete_requested = Signal(str)

    def __init__(
            self,
            view: "TagBar",
            tag_service: TagService,
            asset_tag_service: AssetTagService,
            current_asset_provider: Callable[[], Optional[str]],
            parent: Optional[QObject] = None,
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

        self._adapter = SignalAdapter(self)
        self._connect()

    # ---------------- 绑定 ----------------
    def _connect(self) -> None:
        v = self._view
        self._adapter.connect(v.apply_requested, self._on_apply_requested)
        self._adapter.connect(v.chip_activated, self._on_chip_activated)
        self._adapter.connect(
            v.chip_remove_requested, self._on_chip_remove_requested,
        )
        self._adapter.connect(v.layer_tree_open_requested, self.refresh_tree)
        self._adapter.connect(v.layer_tree_activated, self._on_tree_activated)
        self._adapter.connect(
            v.tag_create_requested, self._on_tag_create_requested,
        )
        self._adapter.connect(
            v.tag_rename_requested, self._on_tag_rename_requested,
        )
        self._adapter.connect(
            v.tag_delete_requested, self._on_tag_delete_requested,
        )

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
        """按 ``父/子`` 路径创建（或复用）标签并绑定到当前资产。

        输入 ``旅行/日本`` 时会补齐整条链；已存在的段直接复用，
        因此重复输入不会报重名，也不会生成重复子树。
        """
        asset_uuid = self._current_asset()
        if asset_uuid is None:
            return
        try:
            tag_uuid = self._tag.ensure_path(text)
        except TagNameConflict as e:
            if e.existing_uuid is None:
                self.error_occurred.emit(
                    self.tr("标签已存在：{text}").format(text=text)
                )
                return
            tag_uuid = e.existing_uuid
        except TagError as e:
            self.error_occurred.emit(str(e))
            return

        try:
            result = self._asset_tag.add(asset_uuid, [tag_uuid])
        except AssetTagError as e:
            self.error_occurred.emit(str(e))
            return

        self._view.clear_apply_input()
        self.refresh()
        self.refresh_tree()
        if result.added > 0:
            self.tags_changed.emit()

    def _on_chip_activated(self, uuid: str) -> None:
        self.tag_activated.emit(uuid)

    def _on_tag_create_requested(self, parent_uuid: str) -> None:
        self.tag_create_requested.emit(parent_uuid)

    def _on_tag_rename_requested(self, uuid: str) -> None:
        self.tag_rename_requested.emit(uuid)

    def _on_tag_delete_requested(self, uuid: str) -> None:
        self.tag_delete_requested.emit(uuid)

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

    # ---------------- 生命周期 ----------------
    def setup(self, _=None) -> None:
        return None

    def teardown(self) -> None:
        self._adapter.disconnect_all()
