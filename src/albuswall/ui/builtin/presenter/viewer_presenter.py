#
""""""

from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID, uuid4

from albuswall.dto.thumbnail import ThumbSpec
from albuswall.services.view import SCOPE_DELETED

from ..model.metadata_model import MetadataModel
from ..delegate.metadata_delegate import MetadataDelegate
from ..widgets.image_loader import (
    ImageLoader, PixmapCache, get_loader, get_cache,
)

if TYPE_CHECKING:
    from PySide6.QtCore import SignalInstance
    from PySide6.QtGui import QPixmap

    from albuswall.dto.album import AssetDTO
    from albuswall.services import ViewService, ThumbnailService
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

    metadata_model: MetadataModel
    metadata_delegate: MetadataDelegate

    def __init__(
            self,
            view: "Detail",
            view_service: "ViewService",
            thumb_service: "ThumbnailService",
            asset_service: "AssetRepository",
            loader: "ImageLoader | None" = None,
            cache: "PixmapCache | None" = None,
    ) -> None:
        self.view: "Detail" = view
        self.view_service: "ViewService" = view_service
        self.thumbnail_service: "ThumbnailService" = thumb_service
        self.asset_service: "AssetRepository" = asset_service

        self.metadata_model = MetadataModel()
        self.metadata_delegate = MetadataDelegate()

        self._loader = loader if loader is not None else get_loader()
        self._cache = cache if cache is not None else get_cache()

        self._ctx: Optional[ViewerContext] = None
        self._is_favorite: bool = False
        self._next_uuid: Optional[str] = None
        self._load_token: Optional[str] = None

        self._connected: bool = False
        self._loader_connected: bool = False

    # ================================================================== #
    # 公开接口
    # ================================================================== #
    def open(self, ctx: ViewerContext) -> bool:
        """进入详情页并展示 ``ctx`` 指定的资产。

        职责仅限于**会话编排**：会话管理 → 定位 → 取数 → 推视图 → 拉图。

        返回：
            True:  大图已从缓存同步命中并推给视图。
            False: 已提交异步请求 / 路径缺失（视图侧走占位）。
        """
        self._begin_session(ctx)
        include_deleted = self._resolve_include_deleted(ctx)
        asset_uuid = str(ctx.current_asset_uuid)

        # 1) 定位邻居（顺带缓存翻页上下文）
        prev_uuid, next_uuid = self._resolve_neighbours(ctx.album_uuid, asset_uuid)

        # 2) 取数（DTO + 磁盘路径）
        dto, full_path = self._load_asset_snapshot(asset_uuid, include_deleted)

        # 3) 推视图（元信息 / 收藏 / 删除态 / 导航栏）
        self._push_metadata(dto, full_path)
        self._push_favorite(dto)
        self._push_deleted(dto)
        self.view.show_information(False)  # 切换资产时复位信息面板
        self._push_navigation(ctx.album_uuid, asset_uuid, include_deleted)

        # 4) 拉大图
        loaded_sync = self._load_full_image(full_path)

        # 5) 预取
        if self.PREFETCH_NEIGHBOURS:
            self._prefetch_neighbours(prev_uuid, next_uuid, include_deleted)

        return loaded_sync

    def refresh(self, *, reload_image: bool = False) -> None:
        """**不切换资产**，把当前 ctx 的数据面从服务层重新拉一遍。

        与 ``open()`` 的区别：
          - 不重置上下文、不重连信号、不重新定位邻居；
          - 不重建导航栏（相册成员未变时无意义）；
          - 默认不重新加载大图字节。

        典型用途：
          - 外部写操作（收藏 / 打标 / EXIF 重写）后追平 UI；
          - 软删 / 恢复后 ``is_deleted``、``deleted_at`` 变化；
          - 磁盘文件被替换时，配合 ``reload_image=True`` 重拉像素。

        Args:
            reload_image: 是否同时重载大图字节。默认 ``False``。
        """
        if self._ctx is None:
            return

        include_deleted = self._resolve_include_deleted(self._ctx)
        asset_uuid = str(self._ctx.current_asset_uuid)

        dto, full_path = self._load_asset_snapshot(asset_uuid, include_deleted)
        self._push_metadata(dto, full_path)
        self._push_favorite(dto)
        self._push_deleted(dto)

        if reload_image:
            self._load_full_image(full_path)

    def navigate(self, asset_uuid: str) -> bool:
        """在当前上下文的相册内切换资产（等价于以新 uuid 重开）。"""
        if self._ctx is None:
            return False
        return self.open(ViewerContext(
            album_uuid=self._ctx.album_uuid,
            current_asset_uuid=str(asset_uuid),
            include_deleted=self._ctx.include_deleted,
        ))

    def hide_detail(self) -> None:
        """把详情视图从栈顶弹出 / 隐藏（动画交由 UI 处理）。"""
        ...

    def setup(self, _) -> None:
        self.view.panel.view.setModel(self.metadata_model)
        self.view.panel.view.setItemDelegate(self.metadata_delegate)

    def teardown(self) -> None:
        self._cancel_current_load()
        self._disconnect_signals()
        self._disconnect_loader()

        self._ctx = None
        self._is_favorite = False
        self._next_uuid = None
        self.view.clear_image()

    # ================================================================== #
    # open() / refresh() 共用的数据面
    # ================================================================== #
    def _load_asset_snapshot(
            self, asset_uuid: str, include_deleted: bool,
    ) -> tuple[Optional["AssetDTO"], Optional[str]]:
        """一次性取回当前资产的 DTO 与磁盘路径（``refresh()`` 复用）。"""
        dto = self.view_service.get_asset(
            asset_uuid, include_deleted=include_deleted,
        )
        full_path = self.view_service.get_asset_full_path(
            asset_uuid, include_deleted=include_deleted,
        )
        return dto, full_path

    def _push_metadata(
            self, dto: Optional["AssetDTO"], full_path: Optional[str],
    ) -> None:
        self.metadata_model.set_metadata(self._build_metadata(dto, full_path))

    def _push_favorite(self, dto: Optional["AssetDTO"]) -> None:
        self._is_favorite = bool(dto.is_favorite) if dto is not None else False
        self.view.set_favorite_state(self._is_favorite)

    def _push_deleted(self, dto: Optional["AssetDTO"]) -> None:
        self.view.set_deleted_state(
            bool(dto.is_deleted) if dto is not None else False
        )

    # ================================================================== #
    # open() 专用步骤
    # ================================================================== #
    def _begin_session(self, ctx: ViewerContext) -> None:
        """开始一次详情会话：撤掉在途加载 → 绑定 ctx → 连好信号。"""
        self._cancel_current_load()
        self._ctx = ctx
        self._ensure_connected()
        self._ensure_loader_connected()

    def _resolve_include_deleted(self, ctx: ViewerContext) -> bool:
        """与 PresenterManager 同口径：Trash 判定走 get_scope。"""
        return (
                ctx.include_deleted
                or self.view_service.get_scope(ctx.album_uuid) == SCOPE_DELETED
        )

    def _resolve_neighbours(
            self, album_uuid: UUID, asset_uuid: str,
    ) -> tuple[Optional[str], Optional[str]]:
        prev_uuid, next_uuid, index, total = \
            self.view_service.get_asset_neighbours(album_uuid, asset_uuid)
        prev_s = str(prev_uuid) if prev_uuid else None
        next_s = str(next_uuid) if next_uuid else None
        self._next_uuid = next_s

        # 缓存翻页上下文（键盘总线上线后可直接消费）
        self.view.set_navigation_position(
            index=index, total=total,
            prev_uuid=prev_s, next_uuid=next_s,
        )
        return prev_s, next_s

    def _push_navigation(
            self, album_uuid: UUID, current_uuid: str, include_deleted: bool,
    ) -> None:
        asset_uuids = [
            str(u) for u in self.view_service.get_asset_uuids(album_uuid)
        ]
        thumb_map = (
            self.thumbnail_service.get_thumbnail_paths(asset_uuids, ThumbSpec.SMALL)
            if asset_uuids else {}
        )
        self.view.set_navigation_items(
            asset_uuids=asset_uuids,
            thumb_map=thumb_map,
            current_asset_uuid=current_uuid,
            include_deleted=include_deleted,
            thumb_service=self.thumbnail_service,
        )

    # ================================================================== #
    # 大图加载
    # ================================================================== #
    def _load_full_image(self, full_path: Optional[str]) -> bool:
        """提交大图加载；返回是否同步命中缓存。"""
        self._cancel_current_load()

        token = uuid4().hex
        self._load_token = token

        if not full_path:
            self.view.clear_image()
            return False

        cached = self._cache.get_full(full_path)
        self._loader.load_full(full_path, token)
        return cached is not None

    def _cancel_current_load(self) -> None:
        if self._load_token is not None:
            self._loader.cancel(self._load_token)
            self._load_token = None

    # ================================================================== #
    # 预加载
    # ================================================================== #
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

    # ================================================================== #
    # 元信息
    # ================================================================== #
    @staticmethod
    def _build_metadata(
            dto: Optional["AssetDTO"], full_path: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if dto is None:
            return None
        return {
            "uuid": str(dto.uuid),
            "original_name": dto.original_name,
            "file_path": full_path or dto.file_path,
            "file_size": dto.file_size,
            "mime_type": dto.mime_type,
            "width": dto.width,
            "height": dto.height,
            "taken_at": dto.taken_at,
            "city": dto.city,
            "created_at": dto.created_at,
            "modified_at": dto.modified_at,
            "is_favorite": dto.is_favorite,
            "is_deleted": dto.is_deleted,
            "deleted_at": dto.deleted_at,
            # "exif": getattr(dto, "exif", None),
        }

    # ================================================================== #
    # 信号
    # ================================================================== #
    def _view_signals(self) -> tuple[tuple["SignalInstance", Any], ...]:
        return (
            (self.view.close_requested, self._on_close_requested),
            (self.view.asset_navigate_requested, self._on_asset_navigate_requested),
            (self.view.favorite_toggle_requested, self._on_favorite_toggle),
            (self.view.trash_requested, self._on_trash_requested),
            (self.view.recover_requested, self._on_recover_requested),
        )

    def _loader_signals(self) -> tuple[tuple["SignalInstance", Any], ...]:
        return (
            (self._loader.full_loaded, self._on_full_loaded),
            (self._loader.load_failed, self._on_load_failed),
        )

    @staticmethod
    def _connect_pairs(pairs: Iterable[tuple["SignalInstance", Any]]) -> None:
        for sig, slot in pairs:
            try:
                sig.connect(slot)
            except (RuntimeError, TypeError):
                _logger.debug("connect failed: %s", slot)

    @staticmethod
    def _disconnect_pairs(pairs: Iterable[tuple["SignalInstance", Any]]) -> None:
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _ensure_connected(self) -> None:
        if self._connected:
            return
        self._connect_pairs(self._view_signals())
        self._connected = True

    def _ensure_loader_connected(self) -> None:
        if self._loader_connected:
            return
        self._connect_pairs(self._loader_signals())
        self._loader_connected = True

    def _disconnect_signals(self) -> None:
        if not self._connected:
            return
        self._disconnect_pairs(self._view_signals())
        self._connected = False

    def _disconnect_loader(self) -> None:
        if not self._loader_connected:
            return
        self._disconnect_pairs(self._loader_signals())
        self._loader_connected = False

    # ================================================================== #
    # 槽
    # ================================================================== #
    def _on_close_requested(self) -> None:
        """视图发出关闭请求 → 先撤掉在途任务。"""
        self._cancel_current_load()

    def _on_asset_navigate_requested(self, asset_uuid: str) -> None:
        """导航栏点击 → 相册内切换资产。"""
        self.navigate(asset_uuid)

    # ---- 图片加载回调 --------------------------------------------------

    def _on_full_loaded(self, token: str, pixmap: "QPixmap") -> None:
        if token != self._load_token:
            return
        self.view.image_viewer.set_pixmap(pixmap)

    def _on_load_failed(self, token: str, path: str) -> None:
        if token != self._load_token:
            return
        self.view.clear_image()

    # ---- 写操作回调 ----------------------------------------------------

    def _on_favorite_toggle(self, target: bool) -> None:
        """收藏按钮点击 → 写库 → ``refresh()`` 追平 → 广播。

        ``target`` 由 View 层直接透传自 ``QPushButton.clicked(bool)``，
        即用户点击后的期望状态，presenter 不再自行取反。
        """
        if self._ctx is None:
            return
        asset_uuid = self._ctx.current_asset_uuid

        try:
            affected = self.asset_service.set_favorite(asset_uuid, target)
        except Exception:
            _logger.exception("set_favorite failed: uuid=%s", asset_uuid)
            return
        if affected <= 0:
            return

        # 走 refresh()：从库中取权威状态，元信息与按钮一起追平
        self.refresh()
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

    def _on_recover_requested(self) -> None:
        """恢复按钮 → 从回收站还原 → ``refresh()`` 追平（当前资产仍在）。"""
        if self._ctx is None:
            return
        asset_uuid = self._ctx.current_asset_uuid

        try:
            self.asset_service.restore(asset_uuid)
        except Exception:
            _logger.exception("restore failed: uuid=%s", asset_uuid)
            return

        # 资产未被移出当前上下文（在 Trash 里原位恢复，仍在同一相册视图），
        # 直接 refresh 更新 is_deleted / deleted_at 与按钮语义。
        self.refresh()
        self.view.notify_assets_changed([asset_uuid])
