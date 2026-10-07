#
""""""

from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from PySide6.QtGui import QPixmap

from albuswall.dto.thumbnail import ThumbSpec
from albuswall.services.view import SCOPE_DELETED

from ..widgets.image_loader import ImageLoader, PixmapCache

if TYPE_CHECKING:
    from concurrent.futures import Future

    from albuswall.services import (
        ViewService,
        ThumbnailService,
    )
    from albuswall.repositories import AssetRepository

    from ..window.detail import Detail

_logger = getLogger(__name__)


@dataclass(slots=True)
class ViewerContext:
    """进入详情页的统一上下文。

    与 PresenterManager 契约一致：presenter 只通过 uuid 与服务层交互。

    Attributes:
        album_uuid: 当前所在相册的 uuid（物理 / 虚拟均可）。
        current_asset_uuid: 要展示资产的 uuid（``assets.uuid``）。
        include_deleted: 是否连同软删资产一起查询。
            Trash 虚拟相册应为 True；其余场景一般 False。
            ``open()`` 内部会与 ``ViewService.get_scope`` 取或。
    """

    album_uuid: UUID
    current_asset_uuid: str
    include_deleted: bool = False


class ViewerPresenter:
    """详情页 Presenter（异步加载版，uuid-only）。"""

    PREFETCH_NEIGHBOURS = True

    def __init__(
            self,
            view: "Detail",
            view_service: "ViewService",
            thumb_service: "ThumbnailService",
            asset_service: "AssetRepository",
            loader: "ImageLoader | None" = None,
            cache: "PixmapCache | None" = None
    ) -> None:
        self.view: "Detail" = view
        self.view_service: "ViewService" = view_service
        self.thumbnail_service: "ThumbnailService" = thumb_service
        self.asset_service: "AssetRepository" = asset_service

        from ..widgets.image_loader import get_loader, get_cache
        self._loader = loader if loader is not None else get_loader()
        self._cache = cache if cache is not None else get_cache()

        self._ctx: Optional[ViewerContext] = None
        self._is_favorite: bool = False
        self._next_uuid: Optional[str] = None

        self._inflight: list["Future"] = []
        self._load_token: Optional[str] = None

        self._connected: bool = False
        self._loader_connected: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self, ctx: ViewerContext) -> bool:
        """进入详情页并展示 ``ctx`` 指定的资产。

        返回：
            True:  大图已从缓存同步命中并推给视图。
            False: 已提交异步请求 / 路径缺失（视图侧走占位）。
        """
        # 0) 丢弃上一个资产遗留的结果
        self._cancel_inflight()
        if self._load_token is not None:
            self._loader.cancel(self._load_token)
            self._load_token = None

        self._ctx = ctx
        self._ensure_connected()
        self._ensure_loader_connected()

        # 与 PresenterManager 同口径：Trash 判定走 get_scope（ViewService
        # 不提供 should_include_deleted）。
        include_deleted = (
                ctx.include_deleted
                or self.view_service.get_scope(ctx.album_uuid) == SCOPE_DELETED
        )

        asset_uuid = str(ctx.current_asset_uuid)

        # ---- 1) 定位：直接走 ViewService.get_asset_neighbours ---------
        prev_uuid, next_uuid, index, total = \
            self.view_service.get_asset_neighbours(
                ctx.album_uuid, asset_uuid,
            )
        self._next_uuid = str(next_uuid) if next_uuid else None
        prev_uuid_s = str(prev_uuid) if prev_uuid else None
        next_uuid_s = str(next_uuid) if next_uuid else None

        # ---- 2) 导航栏缩略图 -----------------------------------------
        asset_uuids: list[str] = [
            str(u)
            for u in self.view_service.get_asset_uuids(ctx.album_uuid)
        ]
        thumb_map: dict[str, str] = {}
        if asset_uuids:
            thumb_map = self.thumbnail_service.get_thumbnail_paths(
                asset_uuids, ThumbSpec.SMALL,
            )

        # ---- 3) 大图路径 ---------------------------------------------
        full_path: Optional[str] = self.view_service.get_asset_full_path(
            asset_uuid, include_deleted=include_deleted,
        )

        # ---- 4) 元信息 -----------------------------------------------
        # ViewService 没有 get_asset_metadata；直接从 AssetDTO 取字段。
        # 若 Detail.set_metadata 需要更多字段，在这里按需扩展即可。
        dto = self.view_service.get_asset(
            asset_uuid, include_deleted=include_deleted,
        )
        metadata: Optional[dict] = None
        if dto is not None:
            metadata = {
                "uuid": str(getattr(dto, "uuid", asset_uuid)),
                "title": getattr(dto, "title", None),
                "description": getattr(dto, "description", None),
                "file_path": getattr(dto, "file_path", None),
                "file_size": getattr(dto, "file_size", None),
                "mime_type": getattr(dto, "mime_type", None),
                "width": getattr(dto, "width", None),
                "height": getattr(dto, "height", None),
                "taken_at": getattr(dto, "taken_at", None),
                "created_at": getattr(dto, "created_at", None),
                "modified_at": getattr(dto, "modified_at", None),
                "is_favorite": bool(getattr(dto, "is_favorite", False)),
                "is_deleted": bool(getattr(dto, "is_deleted", False)),
            }

        # 收藏状态
        self._is_favorite = bool(
            getattr(dto, "is_favorite", False) if dto is not None else False
        )
        self.view.set_favorite_state(self._is_favorite)

        # ---- 5) 推导航栏 / 元信息 -----------------------------------
        self.view.set_navigation_items(
            asset_uuids=asset_uuids,
            thumb_map=thumb_map,
            current_asset_uuid=asset_uuid,
            include_deleted=include_deleted,  # ← 新增
            thumb_service=self.thumbnail_service,  # ← 新增
        )

        # ---- 6) 大图：异步加载 --------------------------------------
        token = uuid4().hex
        self._load_token = token

        if not full_path:
            self.view.set_image("")
            loaded_sync = False
        else:
            cached = self._cache.get_full(full_path)
            self._loader.load_full(full_path, token)
            loaded_sync = cached is not None

        # ---- 7) 预取 prev / next 原图 --------------------------------
        if self.PREFETCH_NEIGHBOURS:
            self._prefetch_neighbours(
                prev_uuid_s, next_uuid_s, include_deleted,
            )

        return loaded_sync

    def navigate(self, asset_uuid: str) -> bool:
        """在当前上下文的相册内切换资产。"""
        if self._ctx is None:
            return False
        return self.open(ViewerContext(
            album_uuid=self._ctx.album_uuid,
            current_asset_uuid=str(asset_uuid),
            include_deleted=self._ctx.include_deleted,
        ))

    def hide_detail(self) -> None:
        """把详情视图从栈顶弹出 / 隐藏。"""
        ...

    def setup(self, _):
        ...

    def teardown(self) -> None:
        self._cancel_inflight()

        if self._load_token is not None:
            self._loader.cancel(self._load_token)
            self._load_token = None

        self._disconnect_signals()
        self._disconnect_loader()

        self._ctx = None
        self._is_favorite = False
        self._next_uuid = None
        self.view.clear_image()

    # ------------------------------------------------------------------ #
    # 预加载
    # ------------------------------------------------------------------ #
    def _prefetch_neighbours(
            self,
            prev_uuid: Optional[str],
            next_uuid: Optional[str],
            include_deleted: bool,
    ) -> None:
        """进入详情后预取上/下一张原图，翻页时可直接命中缓存。"""
        for neighbour_uuid in (prev_uuid, next_uuid):
            if neighbour_uuid is None:
                continue
            try:
                path = self.view_service.get_asset_full_path(
                    neighbour_uuid, include_deleted=include_deleted,
                )
            except Exception:
                # 预加载是 best-effort，任何异常都不该影响当前展示
                continue
            if path:
                self._loader.prefetch_full(path)

    # ------------------------------------------------------------------ #
    # 内部：在途任务
    # ------------------------------------------------------------------ #
    # noinspection broad-exception
    def _cancel_inflight(self) -> None:
        for fut in self._inflight:
            try:
                fut.cancel()
            except Exception:
                pass
        self._inflight.clear()

    def _track(self, future: "Future") -> "Future":
        self._inflight.append(future)
        return future

    # ------------------------------------------------------------------ #
    # 内部：信号
    # ------------------------------------------------------------------ #
    def _ensure_loader_connected(self) -> None:
        if self._loader_connected:
            return
        self._loader.full_loaded.connect(self._on_full_loaded)
        self._loader.load_failed.connect(self._on_load_failed)
        self._loader_connected = True

    def _disconnect_loader(self) -> None:
        if not self._loader_connected:
            return
        for sig, slot in (
                (self._loader.full_loaded, self._on_full_loaded),
                (self._loader.load_failed, self._on_load_failed),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._loader_connected = False

    def _ensure_connected(self) -> None:
        if self._connected:
            return
        self.view.close_requested.connect(self._on_close_requested)
        self.view.favorite_toggle_requested.connect(self._on_favorite_toggle)
        self.view.trash_requested.connect(self._on_trash_requested)
        self._connected = True

    def _disconnect_signals(self) -> None:
        if not self._connected:
            return
        for sig, slot in (
                (self.view.close_requested, self._on_close_requested),
                (self.view.favorite_toggle_requested, self._on_favorite_toggle),
                (self.view.trash_requested, self._on_trash_requested),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connected = False

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_close_requested(self) -> None:
        """视图发出关闭请求 → 先撤掉在途任务。"""
        self._cancel_inflight()
        if self._load_token is not None:
            self._loader.cancel(self._load_token)
            self._load_token = None

    # ---- 图片加载回调 --------------------------------------------------

    def _on_full_loaded(self, token: str, pixmap: QPixmap) -> None:
        if token != self._load_token:
            return
        self.view.image_viewer.set_pixmap(pixmap)

    def _on_load_failed(self, token: str, path: str) -> None:
        if token != self._load_token:
            return
        self.view.set_image("")

    # ---- 写操作回调 ----------------------------------------------------

    def _on_favorite_toggle(self) -> None:
        """收藏按钮点击 → 取反写库 → 刷新按钮 + 广播刷新。"""
        if self._ctx is None:
            return
        asset_uuid = self._ctx.current_asset_uuid
        target = not self._is_favorite

        try:
            affected = self.asset_service.set_favorite(asset_uuid, target)
        except Exception:
            _logger.exception("set_favorite failed: uuid=%s", asset_uuid)
            return
        if affected <= 0:
            return

        self._is_favorite = target
        self.view.set_favorite_state(target)
        self.view.notify_assets_changed([asset_uuid])

    def _on_trash_requested(self) -> None:
        """垃圾桶按钮 → 软删 → 广播 → 跳下一张 / 关闭详情。"""
        if self._ctx is None:
            return
        asset_uuid = self._ctx.current_asset_uuid
        next_uuid = self._next_uuid

        try:
            self.asset_service.soft_delete(asset_uuid)
        except Exception:
            _logger.exception("soft_delete failed: uuid=%s", asset_uuid)
            return

        self.view.notify_assets_changed([asset_uuid])

        if next_uuid is not None:
            self.navigate(next_uuid)
        else:
            self.view.close_requested.emit()
