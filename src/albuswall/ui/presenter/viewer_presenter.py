#
"""详情页 presenter：大图 + 底部导航 + 元信息 + 收藏 / 软删 / 恢复。

职责仅限于**会话编排**
======================

会话管理 → 定位邻居 → 取数 → 推视图 → 拉图。写操作只调
``AssetOpsService``（收藏 / 软删 / 恢复），成功后 :meth:`refresh` 从库里
追平 UI，再广播 ``assets_changed`` 让内容网格刷新 —— presenter 不自己改
本地状态，避免「UI 和库各说各话」。

线程
====

大图与导航条缩略图由 :class:`~albuswall.ui.widgets.image_loader.ImageLoader`
在后台线程解码，回调用 ``token`` 对齐（``full:<uuid>`` / ``nav:<uuid>``），
过期的 token 直接丢弃。缩略图**生成**走 ``ThumbnailService``（按需）。

数据全部走 uuid
===============

``ViewService`` / ``AssetOpsService`` 的入参出参都是 uuid；本模块不接触
``assets.id`` / ``albums.id``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QPixmap

from albuswall.common.enums import ThumbSpec
from albuswall.service.view.service import SCOPE_DELETED
from albuswall.ui.model import NavigationModel

from ..widgets.image_loader import get_loader
from .base import Presenter, register_presenter
from .thumbnails import square_pixmap

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.detail import Detail
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["ViewerContext", "ViewerPresenter"]

logger = logging.getLogger(__package__)

#: 导航条缩略图的解码边长（与 ``Detail`` 的 THUMB_SIZE 一致）。
NAV_EDGE = 80

#: 导航条缩略图规格。
NAV_SPEC = ThumbSpec.SMALL


@dataclass(slots=True)
class ViewerContext:
    """进入详情页的统一上下文。

    Attributes:
        album_uuid: 当前所在相册的 uuid（物理 / 虚拟均可）。
        current_asset_uuid: 要展示资产的 uuid。
        include_deleted: 是否连同软删资产一起查询（回收站场景）。
            ``open()`` 内部会与 ``ViewService.get_scope`` 取或。
    """

    album_uuid: Any
    current_asset_uuid: str
    include_deleted: bool = False


class ViewerPresenter(Presenter):
    """详情页 presenter。"""

    name = "viewer"

    #: 资产被写操作改过（载荷：uuid 字符串序列），内容网格据此刷新。
    assets_changed = Signal(list)
    #: 面向用户的错误 / 提示（窗口 toast）。
    error = Signal(str)
    #: 详情页被关闭。
    closed = Signal()

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._detail: Detail = window.detail
        self._view_service = self.service("view_service")
        self._asset_ops = self.service("asset_ops_service")
        self._thumb_service = self.service("thumbnail_service")
        self._loader = get_loader()

        self._nav_model = NavigationModel(
            icon_size=QSize(NAV_EDGE, NAV_EDGE), parent=self)

        self._ctx: ViewerContext | None = None
        self._is_favorite = False
        self._prev_uuid: str | None = None
        self._next_uuid: str | None = None
        self._nav_uuids: list[str] = []
        self._load_token: str | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        d = self._detail
        d.set_navigation_model(self._nav_model)
        d.close_requested.connect(self._on_close_requested)
        d.asset_navigate_requested.connect(self.navigate)
        d.favorite_toggle_requested.connect(self._on_favorite_toggle)
        d.trash_requested.connect(self._on_trash_requested)
        d.recover_requested.connect(self._on_recover_requested)
        d.edit_requested.connect(self._on_edit_requested)
        d.multiple_choice_requested.connect(self._on_edit_requested)

        if self._loader is not None:
            self._loader.full_loaded.connect(self._on_full_loaded)
            self._loader.thumb_loaded.connect(self._on_thumb_loaded)
            self._loader.load_failed.connect(self._on_load_failed)
        if self._thumb_service is not None:
            self._thumb_service.thumbnail_ready.connect(
                self._on_thumbnail_ready)

    def on_stop(self) -> None:
        self._cancel_current_load()
        if self._loader is not None:
            for signal, slot in (
                    (self._loader.full_loaded, self._on_full_loaded),
                    (self._loader.thumb_loaded, self._on_thumb_loaded),
                    (self._loader.load_failed, self._on_load_failed),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        if self._thumb_service is not None:
            try:
                self._thumb_service.thumbnail_ready.disconnect(
                    self._on_thumbnail_ready)
            except (RuntimeError, TypeError):
                pass

    def retranslate(self) -> None:
        """语言切换：信息面板的字段标签由模型自己重建。"""
        model = self._detail.panel.metadata_model()
        if model is not None and hasattr(model, "retranslate"):
            model.retranslate()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def context(self) -> ViewerContext | None:
        return self._ctx

    @property
    def current_asset_uuid(self) -> str | None:
        return None if self._ctx is None else self._ctx.current_asset_uuid

    def open(self, ctx: ViewerContext) -> bool:
        """进入详情页并展示 ``ctx`` 指定的资产，返回是否同步命中图片缓存。"""
        self._ctx = ctx
        include_deleted = self._resolve_include_deleted(ctx)
        asset_uuid = str(ctx.current_asset_uuid)

        prev_uuid, next_uuid = self._resolve_neighbours(
            ctx.album_uuid, asset_uuid)
        self._prev_uuid, self._next_uuid = prev_uuid, next_uuid

        asset = self._load_asset(asset_uuid, include_deleted)
        full_path = self._load_full_path(asset_uuid, include_deleted)

        self._push_metadata(asset)
        self._push_states(asset)
        self._detail.show_information(False)
        self._push_navigation(ctx.album_uuid, asset_uuid)

        loaded_sync = self._load_full_image(full_path)
        self._prefetch_neighbours(prev_uuid, next_uuid, include_deleted)
        return loaded_sync

    def refresh(self, *, reload_image: bool = False) -> None:
        """不切换资产，把当前资产的数据面从服务层重新拉一遍。"""
        if self._ctx is None:
            return
        include_deleted = self._resolve_include_deleted(self._ctx)
        asset_uuid = str(self._ctx.current_asset_uuid)

        asset = self._load_asset(asset_uuid, include_deleted)
        self._push_metadata(asset)
        self._push_states(asset)
        if reload_image:
            self._load_full_image(
                self._load_full_path(asset_uuid, include_deleted))

    def navigate(self, asset_uuid: str) -> bool:
        """在当前相册内切换到指定资产。"""
        if self._ctx is None:
            return False
        target = str(asset_uuid)
        if target == self._ctx.current_asset_uuid:
            return False
        return self.open(ViewerContext(
            album_uuid=self._ctx.album_uuid,
            current_asset_uuid=target,
            include_deleted=self._ctx.include_deleted,
        ))

    def navigate_relative(self, offset: int) -> bool:
        """按相册顺序翻页（-1 上一张 / +1 下一张）。"""
        target = self._next_uuid if offset >= 0 else self._prev_uuid
        if not target:
            return False
        return self.navigate(target)

    def close(self) -> None:
        """退出详情页：撤掉在途加载并清空视图。"""
        self._cancel_current_load()
        self._ctx = None
        self._prev_uuid = None
        self._next_uuid = None
        self._detail.clear_image()
        self._detail.clear_navigation()
        self._detail.set_metadata(None)

    # ------------------------------------------------------------------ #
    # 数据面
    # ------------------------------------------------------------------ #
    def _resolve_include_deleted(self, ctx: ViewerContext) -> bool:
        if ctx.include_deleted:
            return True
        if self._view_service is None:
            return False
        try:
            return self._view_service.get_scope(ctx.album_uuid) == SCOPE_DELETED
        except Exception:  # noqa: BLE001
            logger.exception("viewer presenter: get_scope failed")
            return False

    def _load_asset(self, asset_uuid: str, include_deleted: bool) -> Any | None:
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_asset(
                asset_uuid, include_deleted=include_deleted)
        except Exception:  # noqa: BLE001
            logger.exception(
                "viewer presenter: get_asset failed: %s", asset_uuid)
            return None

    def _load_full_path(
            self, asset_uuid: str, include_deleted: bool,
    ) -> str | None:
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_asset_full_path(
                asset_uuid, include_deleted=include_deleted)
        except Exception:  # noqa: BLE001
            logger.exception(
                "viewer presenter: get_asset_full_path failed: %s", asset_uuid)
            return None

    def _push_metadata(self, asset: Any | None) -> None:
        self._detail.set_metadata(asset)

    def _push_states(self, asset: Any | None) -> None:
        self._is_favorite = bool(
            getattr(asset, "is_favorite", False)) if asset is not None else False
        self._detail.set_favorite_state(self._is_favorite)
        self._detail.set_deleted_state(
            bool(getattr(asset, "is_deleted", False))
            if asset is not None else False)

    def _resolve_neighbours(
            self, album_uuid: Any, asset_uuid: str,
    ) -> tuple[str | None, str | None]:
        if self._view_service is None:
            return None, None
        try:
            prev_uuid, next_uuid, _index, _total = (
                self._view_service.get_asset_neighbours(
                    album_uuid, asset_uuid)
            )
        except Exception:  # noqa: BLE001
            logger.exception("viewer presenter: get_asset_neighbours failed")
            return None, None
        return (
            str(prev_uuid) if prev_uuid else None,
            str(next_uuid) if next_uuid else None,
        )

    # ------------------------------------------------------------------ #
    # 导航条
    # ------------------------------------------------------------------ #
    def _push_navigation(self, album_uuid: Any, current_uuid: str) -> None:
        if self._view_service is None:
            return
        try:
            uuids = [
                str(u)
                for u in self._view_service.get_asset_uuids(album_uuid)
            ]
        except Exception:  # noqa: BLE001
            logger.exception("viewer presenter: navigation list failed")
            return
        self._nav_uuids = uuids
        self._detail.set_navigation_items(
            [(u, None) for u in uuids], current=current_uuid)

        self._load_nav_thumbs()

    def _load_nav_thumbs(self) -> None:
        """把导航条缩略图灌进模型（有路径就解码，没有就请求生成）。"""
        if self._thumb_service is None:
            return
        for asset_uuid in self._nav_uuids:
            if self._nav_model.pixmap_at(
                    self._nav_model.row_of(asset_uuid)) is not None:
                continue
            path = self._nav_path(asset_uuid)
            if path is None:
                continue
            self._loader.load_thumb(
                path, NAV_EDGE, f"nav:{asset_uuid}")

    def _nav_path(self, asset_uuid: str) -> str | None:
        if self._thumb_service is None:
            return None
        try:
            path = self._thumb_service.get_thumbnail_path(
                asset_uuid, NAV_SPEC)
        except Exception:  # noqa: BLE001
            logger.exception("viewer presenter: nav thumb path failed")
            return None
        if path:
            return path
        try:
            self._thumb_service.request(
                asset_uuid,
                include_deleted=self._resolve_include_deleted(
                    self._ctx) if self._ctx is not None else False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("viewer presenter: nav thumb request failed")
        return None

    # ------------------------------------------------------------------ #
    # 大图
    # ------------------------------------------------------------------ #
    def _load_full_image(self, full_path: str | None) -> bool:
        self._cancel_current_load()
        token = uuid4().hex
        self._load_token = token
        if not full_path or self._loader is None:
            self._detail.clear_image()
            return False
        cached = None
        try:
            cached = self._loader.cache.get_full(full_path)
        except Exception:  # noqa: BLE001
            cached = None
        self._loader.load_full(full_path, token)
        return cached is not None

    def _cancel_current_load(self) -> None:
        if self._load_token is not None and self._loader is not None:
            self._loader.cancel(self._load_token)
            self._load_token = None

    def _prefetch_neighbours(
            self,
            prev_uuid: str | None,
            next_uuid: str | None,
            include_deleted: bool,
    ) -> None:
        """进入详情后预取上/下一张原图（best-effort，失败无影响）。"""
        if self._loader is None:
            return
        for neighbour_uuid in (prev_uuid, next_uuid):
            if neighbour_uuid is None:
                continue
            path = self._load_full_path(neighbour_uuid, include_deleted)
            if path:
                self._loader.prefetch_full(path)

    # ------------------------------------------------------------------ #
    # 槽：加载结果
    # ------------------------------------------------------------------ #
    def _on_full_loaded(self, token: str, pixmap: QPixmap) -> None:
        if token != self._load_token:
            return
        self._detail.set_image(pixmap)

    def _on_load_failed(self, token: str, _path: str) -> None:
        if token != self._load_token:
            return
        self._detail.clear_image()

    def _on_thumb_loaded(self, token: str, pixmap: QPixmap) -> None:
        if not token.startswith("nav:"):
            return
        asset_uuid = token[len("nav:"):]
        if asset_uuid in self._nav_uuids:
            # 导航条一格是正方形：缩略图文件不是方的，先居中裁方再回填，
            # 否则 QListView 的 IconMode 会保持宽高比、留下空白边。
            self._nav_model.set_pixmap_for(
                asset_uuid, square_pixmap(pixmap, edge=NAV_EDGE))

    def _on_thumbnail_ready(self, _sender: object, *, asset_uuid: str) -> None:
        """缩略图生成完成：导航条上那一格补一次解码。"""
        asset_uuid = str(asset_uuid)
        if asset_uuid not in self._nav_uuids or self._loader is None:
            return
        path = self._nav_path(asset_uuid)
        if path:
            self._loader.load_thumb(path, NAV_EDGE, f"nav:{asset_uuid}")

    # ------------------------------------------------------------------ #
    # 槽：视图意图
    # ------------------------------------------------------------------ #
    def _on_close_requested(self) -> None:
        self._cancel_current_load()
        self.closed.emit()

    def _on_favorite_toggle(self, target: bool) -> None:
        """收藏按钮点击：写库 → 从库里追平 → 通知内容网格刷新。"""
        asset_uuid = self.current_asset_uuid
        if asset_uuid is None or self._asset_ops is None:
            return
        try:
            result = self._asset_ops.set_favorite(asset_uuid, bool(target))
        except Exception as exc:  # noqa: BLE001
            logger.exception("viewer presenter: set_favorite failed")
            self.error.emit(str(exc))
            return
        if getattr(result, "outcome", "") == "not_found":
            return
        self.refresh()
        self.assets_changed.emit([asset_uuid])

    def _on_trash_requested(self) -> None:
        """移入回收站：写库 → 通知网格 → 跳到下一张 / 关闭详情。"""
        asset_uuid = self.current_asset_uuid
        if asset_uuid is None or self._asset_ops is None:
            return
        next_uuid = self._next_uuid
        try:
            result = self._asset_ops.trash([asset_uuid])
        except Exception as exc:  # noqa: BLE001
            logger.exception("viewer presenter: trash failed")
            self.error.emit(str(exc))
            return
        changed = [str(u) for u in (getattr(result, "changed", ()) or ())]
        if not changed:
            return
        self.assets_changed.emit(changed)
        if next_uuid:
            self.navigate(next_uuid)
        else:
            self._detail.close_requested.emit()

    def _on_recover_requested(self) -> None:
        """从回收站恢复：写库 → 追平当前展示（资产仍在同一视图里）。"""
        asset_uuid = self.current_asset_uuid
        if asset_uuid is None or self._asset_ops is None:
            return
        try:
            result = self._asset_ops.restore([asset_uuid])
        except Exception as exc:  # noqa: BLE001
            logger.exception("viewer presenter: restore failed")
            self.error.emit(str(exc))
            return
        restored = [str(u) for u in (getattr(result, "restored", ()) or ())]
        if restored:
            self.assets_changed.emit(restored)
        self.refresh()

    def _on_edit_requested(self) -> None:
        self.error.emit(self.tr("该功能尚未实现"))


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_viewer_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("viewer presenter skipped: no window bound")
        return None
    return ViewerPresenter(container, window=window, parent=main)
