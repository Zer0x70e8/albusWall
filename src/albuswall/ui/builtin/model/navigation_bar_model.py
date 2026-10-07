#
"""横向缩略图导航栏的 ``QAbstractListModel`` 实现。

缩略图按需加载：只有当视图调用 ``data(idx, DecorationRole)`` 时
才向 ``ImageLoader`` 提交该索引的缩略图请求，避免一次性把几千张
缩略图塞进内存。

与 ``ThumbnailService`` 协作（遵循其公开契约）：
    · ``thumbnail_ready`` 广播 **asset uuid**（而非 token）；
    · ``submit_by_uuid(uuid, *, include_deleted)`` 在去重命中时返回 ``None``；
    · ``get_thumbnail_paths([uuid], spec)`` 是读取路径的唯一入口。

    · 磁盘上尚无缩略图（``thumb_map`` 缺失该 uuid，或 ImageLoader 加载失败）
      → 通过 ``submit_by_uuid`` 提交生成；
    · 收到 ``thumbnail_ready(uuid)`` → 重新解析路径 → ``dataChanged`` 触发重绘；
    · 同一 uuid 的生成尝试上限 ``_MAX_GENERATION_ATTEMPTS``，命中后静默
      放弃，避免与 ImageLoader 的错误路径形成死循环。

契约（与 ViewService / PresenterManager 一致）：
    - 全部入参 / 出参只走 asset uuid 字符串；``assets.id`` 不暴露。
    - ``thumb_map`` 的 key 为 uuid 字符串。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence, cast
from uuid import uuid4

from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QSize, Qt, Signal, Slot,
)
from PySide6.QtGui import QPixmap

from albuswall.dto.thumbnail import ThumbSpec
from albuswall.log import Logger, getLogger

from ..widgets.image_loader import ImageLoader, get_loader

_logger: Logger = getLogger(__name__)

#: 同一 asset uuid 的生成尝试上限。与 ``image_presenter`` 保持一致。
_MAX_GENERATION_ATTEMPTS = 3


class ThumbnailModel(QAbstractListModel):
    """供 ``Detail.item_line_viewer`` 使用的缩略图模型。

    Roles:
        ``DecorationRole``  → 缩略图 ``QPixmap``（懒加载，未就绪时返回 ``None``）。
        ``UserRole``        → ``asset_uuid``（str）。
        ``ToolTipRole``     → uuid 短前缀 + 缩略图路径。

    懒加载策略：
        - ``data(DecorationRole)`` 被调用时若该行尚无 pixmap，则向
          ``ImageLoader.load_thumb`` 提交一次异步请求，并在
          ``_row_to_token`` 里登记，保证同一行不会重复提交；
        - 加载完成信号 ``thumb_loaded`` 到达时按 token 找到行号，
          缓存 pixmap 并 ``dataChanged`` 通知视图重绘该格；
        - 只缓存已经渲染过的行；未滚到视口中的行不会被请求。

    生成协作：
        - 路径缺失 / 加载失败 → ``ThumbnailService.submit_by_uuid``；
        - ``thumbnail_ready`` 广播 → 重新读路径 → 触发懒加载；
        - 尝试次数达上限后静默放弃（占位保持空白）。

    当前项高亮：
        本模型只记录 ``_current_index``，实际高亮由 ``DetailOverlay``
        的 QSS（``QListView::item:selected``）负责渲染。
    """

    # 缩略图单元格建议尺寸；具体由 delegate / QSS 决定
    THUMB_SIZE = QSize(80, 80)

    #: 用于把 ``ThumbnailService.thumbnail_ready``（可能从 worker 线程发出）
    #: 排队切回主线程。
    _thumbnail_ready_bridge = Signal(str)

    #: 请求路径时使用的规格；与 ViewerPresenter 里的 ThumbSpec.SMALL 对应。
    _SPEC: ThumbSpec = ThumbSpec.SMALL

    def __init__(
            self,
            loader: Optional[ImageLoader] = None,
            thumb_service: Optional[Any] = None,
            parent=None,
    ) -> None:
        super().__init__(parent=parent)

        # get_loader() 在类型上可能返回 Optional，这里 cast 一把
        self._loader: ImageLoader = (
            loader if loader is not None
            else cast(ImageLoader, get_loader())
        )
        self._thumb_service: Optional[Any] = thumb_service

        # ---- 数据 --------------------------------------------------------
        self._asset_uuids: list[str] = []
        # 与 _asset_uuids 一一对应；缺项时为 ""（占位）
        self._thumb_paths: list[str] = []
        self._include_deleted: bool = False

        # ---- 缩略图缓存 / 在途请求 --------------------------------------
        self._pixmaps: dict[int, QPixmap] = {}  # row -> 已就绪 QPixmap
        self._row_to_token: dict[int, str] = {}  # row -> token
        self._token_to_row: dict[str, int] = {}  # token -> row

        # ---- 生成任务账本 -----------------------------------------------
        # uuid -> 已尝试次数。命中 thumbnail_ready 或加载成功时清零。
        self._generation_attempts: dict[str, int] = {}

        self._current_index: int = -1
        self._connected: bool = False
        self._service_connected: bool = False
        self._service_relay: Optional[Callable[[str], None]] = None

        self._thumbnail_ready_bridge.connect(
            self._on_thumbnail_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self._connect_loader()
        self._connect_service()

    # ------------------------------------------------------------------ #
    # QAbstractListModel
    # ------------------------------------------------------------------ #
    # noinspection PyMethodOverriding
    def rowCount(  # noqa: N802
            self,
            parent: QModelIndex = QModelIndex(),
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._asset_uuids)

    # noinspection PyMethodOverriding
    def data(  # noqa: N802
            self,
            index: QModelIndex,
            role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._asset_uuids):
            return None

        if role == Qt.ItemDataRole.UserRole:
            return self._asset_uuids[row]

        if role == Qt.ItemDataRole.ToolTipRole:
            path = self._thumb_paths[row] if row < len(self._thumb_paths) else ""
            asset_uuid = self._asset_uuids[row]
            short = asset_uuid[:8] if asset_uuid else ""
            return f"#{short}\n{path}" if path else f"#{short}"

        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmap_for_row(row)

        if role == Qt.ItemDataRole.SizeHintRole:
            return self.THUMB_SIZE

        return None

    # noinspection PyMethodOverriding
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        # 用 .value 走 int 位或再包回 ItemFlag：避开 PySide6 stub 里
        # ``ItemIsSelectable`` 被声明为 Literal 导致 ``__or__`` 报错的问题。
        return Qt.ItemFlag(
            Qt.ItemFlag.ItemIsEnabled.value | Qt.ItemFlag.ItemIsSelectable.value
        )

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def set_thumb_service(self, service: Optional[Any]) -> None:
        """注入 / 替换 ``ThumbnailService``；幂等。"""
        if service is self._thumb_service:
            return
        self._disconnect_service()
        self._thumb_service = service
        self._connect_service()

    def set_items(
            self,
            asset_uuids: Sequence[str],
            thumb_map: Mapping[str, str],
            *,
            include_deleted: bool = False,
    ) -> None:
        """重建模型；``thumb_map`` 里缺失的 asset uuid 用空路径占位。

        Args:
            asset_uuids: 按相册排序契约排好的 uuid 序列。
            thumb_map: uuid -> 缩略图磁盘路径。
            include_deleted:
                当前视图是否为 Trash。会随生成任务一起交给
                ``ThumbnailService.submit_by_uuid``。
        """
        # 旧数据里的在途请求失效，先取消
        self._cancel_inflight()

        self.beginResetModel()
        self._asset_uuids = [str(u) for u in asset_uuids]
        self._thumb_paths = [
            (thumb_map.get(uid) or "") for uid in self._asset_uuids
        ]
        self._include_deleted = bool(include_deleted)
        self._pixmaps.clear()
        self._generation_attempts.clear()
        self._current_index = -1
        self.endResetModel()

    def set_current_index(self, index: int) -> None:
        """记录当前高亮的行号（实际高亮由 DetailOverlay 的 QSS 渲染）。"""
        if index == self._current_index:
            return
        old = self._current_index
        self._current_index = index

        for row in (old, index):
            if 0 <= row < len(self._asset_uuids):
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx)

    def current_index(self) -> int:
        return self._current_index

    def asset_uuid_at(self, row: int) -> Optional[str]:
        """返回该行对应的 asset uuid；越界返回 None。"""
        if 0 <= row < len(self._asset_uuids):
            return self._asset_uuids[row]
        return None

    # 兼容旧调用方；建议逐步迁移到 asset_uuid_at
    asset_id_at = asset_uuid_at

    def row_of(self, asset_uuid: str) -> int:
        """返回 ``asset_uuid`` 对应的行号；找不到时返回 -1。"""
        try:
            return self._asset_uuids.index(str(asset_uuid))
        except ValueError:
            return -1

    def clear(self) -> None:
        self.set_items([], {})

    def shutdown(self) -> None:
        """断开与 ``ImageLoader`` / ``ThumbnailService`` 的连接；可在窗口销毁前调用。"""
        self._cancel_inflight()
        self._disconnect_loader()
        self._disconnect_service()
        self._pixmaps.clear()
        self._generation_attempts.clear()

    # ------------------------------------------------------------------ #
    # 懒加载实现
    # ------------------------------------------------------------------ #
    def _pixmap_for_row(self, row: int) -> Optional[QPixmap]:
        cached = self._pixmaps.get(row)
        if cached is not None:
            return cached

        # 已经提交过请求 → 等信号，别重复提交
        if row in self._row_to_token:
            return None

        path = self._thumb_paths[row] if row < len(self._thumb_paths) else ""
        if not path:
            # 磁盘上没有 → 交给 ThumbnailService 生成（service 内部去重）。
            self._submit_generation(row)
            return None

        token = uuid4().hex
        self._row_to_token[row] = token
        self._token_to_row[token] = row

        # 命中缓存时 load_thumb 会同步 emit → _on_thumb_loaded 已填好。
        # 尺寸直接取 THUMB_SIZE，避免与 delegate 的大小脱节。
        self._loader.load_thumb(
            path, token=token, size=self.THUMB_SIZE.width(),
        )
        return self._pixmaps.get(row)

    # noinspection PyBroadException
    def _cancel_inflight(self) -> None:
        for token in self._row_to_token.values():
            try:
                self._loader.cancel(token=token)
            except Exception:  # noqa: BLE001  —— 取消是 best-effort
                pass
        self._row_to_token.clear()
        self._token_to_row.clear()

    # ------------------------------------------------------------------ #
    # 生成：交给 ThumbnailService
    # ------------------------------------------------------------------ #
    def _submit_generation(self, row: int) -> None:
        if self._thumb_service is None:
            return
        if not 0 <= row < len(self._asset_uuids):
            return

        asset_uuid = self._asset_uuids[row]
        attempts = self._generation_attempts.get(asset_uuid, 0)
        if attempts >= _MAX_GENERATION_ATTEMPTS:
            # 到上限后静默放弃；视图保持空白占位，避免死循环。
            return

        self._generation_attempts[asset_uuid] = attempts + 1
        try:
            # ThumbnailService 按 (uuid, version) 内部去重；
            # 重复提交时返回 None，只需等 thumbnail_ready 广播即可。
            self._thumb_service.submit_by_uuid(
                asset_uuid,
                include_deleted=self._include_deleted,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "submit thumbnail failed uuid=%s", asset_uuid,
            )

    def _resolve_path(self, asset_uuid: str) -> str:
        """向 service 重新读一次该 uuid 的缩略图路径；失败返回 ""。"""
        if self._thumb_service is None:
            return ""
        try:
            paths = self._thumb_service.get_thumbnail_paths(
                [asset_uuid], self._SPEC,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "resolve thumbnail path failed uuid=%s", asset_uuid,
            )
            return ""
        return paths.get(asset_uuid, "") or ""

    # ------------------------------------------------------------------ #
    # ImageLoader 信号
    # ------------------------------------------------------------------ #
    def _connect_loader(self) -> None:
        if self._connected:
            return
        self._loader.thumb_loaded.connect(self._on_thumb_loaded)
        self._loader.load_failed.connect(self._on_load_failed)
        self._connected = True

    def _disconnect_loader(self) -> None:
        if not self._connected:
            return
        for sig, slot in (
                (self._loader.thumb_loaded, self._on_thumb_loaded),
                (self._loader.load_failed, self._on_load_failed),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connected = False

    def _on_thumb_loaded(self, token: str, pixmap: QPixmap) -> None:
        row = self._token_to_row.pop(token, None)
        if row is None:
            # 过期 / 已被 reset 掉的请求
            return
        self._row_to_token.pop(row, None)

        if not 0 <= row < len(self._asset_uuids):
            return

        self._pixmaps[row] = pixmap
        # 加载成功 → 该 uuid 的生成尝试账本清零
        self._generation_attempts.pop(self._asset_uuids[row], None)

        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def _on_load_failed(self, token: str, path: str) -> None:  # noqa: ARG002
        row = self._token_to_row.pop(token, None)
        if row is None:
            return
        self._row_to_token.pop(row, None)

        if not 0 <= row < len(self._asset_uuids):
            return

        # 路径存在但 ImageLoader 加载失败 → 清空路径，交给 service 修复
        # （生成失败会被 _MAX_GENERATION_ATTEMPTS 兜底）。
        if row < len(self._thumb_paths) and self._thumb_paths[row] == path:
            self._thumb_paths[row] = ""
        self._submit_generation(row)

    # ------------------------------------------------------------------ #
    # ThumbnailService 信号
    # ------------------------------------------------------------------ #
    def _connect_service(self) -> None:
        if self._service_connected or self._thumb_service is None:
            return
        signal = getattr(self._thumb_service, "thumbnail_ready", None)
        if signal is None:
            return

        # 用 relay 保存引用，便于断开；走 bridge 队列切回主线程。
        def relay(asset_uuid: str) -> None:
            self._thumbnail_ready_bridge.emit(asset_uuid)

        self._service_relay = relay
        try:
            signal.connect(relay)
            self._service_connected = True
        except (RuntimeError, TypeError):
            self._service_relay = None

    def _disconnect_service(self) -> None:
        if not self._service_connected or self._thumb_service is None:
            self._service_relay = None
            self._service_connected = False
            return
        signal = getattr(self._thumb_service, "thumbnail_ready", None)
        if signal is not None and self._service_relay is not None:
            try:
                signal.disconnect(self._service_relay)
            except (RuntimeError, TypeError):
                pass
        self._service_relay = None
        self._service_connected = False

    @Slot(str)
    def _on_thumbnail_ready(self, asset_uuid: str) -> None:
        """生成完成 → 重新读路径 → 触发懒加载重绘。

        - 不清理 ``_generation_attempts``：成功加载后会由 ``_on_thumb_loaded``
          清零；这里是"路径已就绪"的事件，不代表 pixmap 已缓存。
        - 若此刻行已有 pixmap（竞态 / 用户已在别处加载），直接跳过。
        """
        row = self.row_of(asset_uuid)
        if row < 0:
            return  # 已不在当前数据源
        if row in self._pixmaps:
            return

        new_path = self._resolve_path(asset_uuid)
        if not new_path:
            # 广播到达但路径仍不可读 —— 交给尝试上限兜底，不在这里循环。
            return

        self._thumb_paths[row] = new_path
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
