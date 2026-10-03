#
""""""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from PySide6.QtGui import QPixmap

from albuswall.dto.thumbnail import ThumbSpec

from ..widgets.image_loader import ImageLoader, PixmapCache

if TYPE_CHECKING:
    from concurrent.futures import Future

    from albuswall.services import (
        ViewService,  # 提供虚拟相簿抽象
        ThumbnailService,
    )
    from albuswall.repositories import AssetRepository

    from ..window.detail import Detail


@dataclass(slots=True)
class ViewerContext:
    """进入详情页的统一上下文。

    调用方（缩略图网格、收藏列表、搜索结果等）只需构造此 dataclass
    再调用 ``ViewerPresenter.open(ctx)``，即可进入详情页。

    Attributes:
        album_uuid: 当前所在相册的 uuid（物理 / 虚拟均可），
            供上/下一张定位与缩略图导航栏复用同一排序契约。
        current_asset_id: 要展示资产的整数主键（``assets.id``）。
        include_deleted: 是否连同软删资产一起查询。
            Trash 虚拟相册应为 True；其余场景一般 False。
            ``open()`` 内部会与 ``ViewService.should_include_deleted`` 取或，
            所以调用方即使漏传，Trash 场景也会被兜住。
    """

    album_uuid: UUID
    current_asset_id: int
    include_deleted: bool = False


class ViewerPresenter:
    """详情页 Presenter（异步加载版）。"""

    # 预加载：进入详情后自动预热 prev / next 的原图
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

        # 当前资产的收藏状态（open 时从 dto 同步）
        self._is_favorite: bool = False

        # 本次导航的下一张 id（trash 后用于决定跳哪 / 是否关闭）
        self._next_id: Optional[int] = None

        self._inflight: list["Future"] = []
        self._load_token: Optional[str] = None

        self._connected: bool = False
        self._loader_connected: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self, ctx: ViewerContext) -> bool:
        """进入详情页并展示 ``ctx`` 指定的资产。

        与旧版差异：大图改为 ``ImageLoader.load_full`` 异步加载，
        结果通过 ``_on_full_loaded`` 回到 GUI 线程后 ``view.set_pixmap(...)``。

        返回：
            True:  图片已从缓存**同步**命中并已推给视图。
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

        include_deleted = (
                ctx.include_deleted
                or self.view_service.should_include_deleted(ctx.album_uuid)
        )

        # ---- 1) 定位：当前资产在相册里的上/下一张与序号 --------------
        prev_id, next_id, index, total = self.view_service.get_asset_neighbours(
            ctx.album_uuid, ctx.current_asset_id,
        )
        self._next_id = next_id

        # ---- 2) 导航栏缩略图 -----------------------------------------
        asset_ids: list[int] = self.view_service.get_asset_ids(ctx.album_uuid)
        thumb_map: dict[int, str] = {}
        if asset_ids:
            thumb_map = self.thumbnail_service.get_thumbnail_paths(
                asset_ids, ThumbSpec.SMALL,
            )

        # ---- 3) 大图路径 ---------------------------------------------
        full_path: Optional[str] = self.view_service.get_asset_full_path_by_id(
            ctx.current_asset_id,
            include_deleted=include_deleted,
        )

        # ---- 4) 元信息 -----------------------------------------------
        metadata: Optional[dict] = None
        dto = self.view_service.get_asset_by_id(
            ctx.current_asset_id, include_deleted=include_deleted,
        )
        if dto is not None:
            metadata = self.view_service.get_asset_metadata(
                dto.uuid, include_deleted=include_deleted,
            )

        # 收藏状态：优先取 dto，取不到就当作未收藏
        self._is_favorite = bool(
            getattr(dto, "is_favorite", False) if dto is not None else False
        )
        self.view.set_favorite_state(self._is_favorite)

        # ---- 5) 推导航栏 / 元信息 -----------------------------------
        self.view.set_navigation_items(
            asset_ids=asset_ids,
            thumb_map=thumb_map,
            current_asset_id=ctx.current_asset_id,
        )
        self.view.set_navigation_position(
            index=index, total=total,
            prev_id=prev_id, next_id=next_id,
        )
        if metadata is not None:
            self.view.set_metadata(metadata)

        # ---- 6) 大图：异步加载，每个请求一个新 token ----------------
        token = uuid4().hex
        self._load_token = token

        if not full_path:
            # 路径缺失 → 直接切占位图
            self.view.set_image("")
            loaded_sync = False
        else:
            cached = self._cache.get_full(full_path)
            # 命中缓存时 load_full 会同步 emit full_loaded → _on_full_loaded
            self._loader.load_full(full_path, token)
            loaded_sync = cached is not None

        # ---- 7) 预取 prev / next 原图 --------------------------------
        if self.PREFETCH_NEIGHBOURS:
            self._prefetch_neighbours(prev_id, next_id, include_deleted)

        return loaded_sync

    def navigate(self, asset_id: int) -> bool:
        """在当前上下文的相册内切换资产。"""
        if self._ctx is None:
            return False
        return self.open(ViewerContext(
            album_uuid=self._ctx.album_uuid,
            current_asset_id=int(asset_id),
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
        self._next_id = None
        self.view.clear_image()

    # ------------------------------------------------------------------ #
    # 预加载
    # ------------------------------------------------------------------ #
    def _prefetch_neighbours(
            self,
            prev_id: Optional[int],
            next_id: Optional[int],
            include_deleted: bool,
    ) -> None:
        """进入详情后预取上/下一张原图，翻页时可直接命中缓存。"""
        for neighbour_id in (prev_id, next_id):
            if neighbour_id is None:
                continue
            try:
                path = self.view_service.get_asset_full_path_by_id(
                    neighbour_id, include_deleted=include_deleted,
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

    # ---- 图片加载回调 ------------------------------------------------------

    def _on_full_loaded(self, token: str, pixmap: QPixmap) -> None:
        """大图加载完成。过期 token 直接丢弃。"""
        if token != self._load_token:
            return
        self.view.image_viewer.set_pixmap(pixmap)

    def _on_load_failed(self, token: str, path: str) -> None:
        """大图加载失败 / 路径缺失 → 切占位图。"""
        if token != self._load_token:
            return
        self.view.set_image("")

    # ---- 写操作回调 --------------------------------------------------------

    def _on_favorite_toggle(self) -> None:
        """收藏按钮点击 → 取反写库 → 刷新按钮 + 广播刷新。"""
        if self._ctx is None:
            return
        asset_id = self._ctx.current_asset_id
        target = not self._is_favorite

        try:
            affected = self.asset_service.set_favorite(asset_id, target)
        except Exception:
            return
        if affected <= 0:
            # 资产不存在：不刷按钮，也不广播
            return

        self._is_favorite = target
        self.view.set_favorite_state(target)
        self.view.notify_assets_changed([asset_id])

    def _on_trash_requested(self) -> None:
        """垃圾桶按钮 → 软删 + 缩略图清理 → 跳下一张 / 关闭详情。

        顺序（与需求一致）：
            1) asset_service.soft_delete(id)
            2) thumbnail_service.purge_bulk([id])
            3) 广播 assets_changed
            4) 有下一张 → navigate(next_id)；没有 → close_requested
        """
        if self._ctx is None:
            return
        asset_id = self._ctx.current_asset_id
        next_id = self._next_id  # 本帧导航时的下一张，删除后仍以它为准

        try:
            self.asset_service.soft_delete(asset_id)
        except Exception:
            return

        # NOTE: 软删除不purge_bulk
        # # 缩略图清理是 best-effort：库里行删了就该走完流程，
        # # 不要因为某张缩略图文件删失败而卡住 UI。
        # try:
        #     self.thumbnail_service.purge_bulk([asset_id])
        # except Exception:
        #     pass

        self.view.notify_assets_changed([asset_id])

        if next_id is not None:
            self.navigate(next_id)
        else:
            # 已经到头：让上层把详情弹掉
            self.view.close_requested.emit()
