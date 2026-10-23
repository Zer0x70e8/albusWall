#
"""虚拟滚动网格 ⇄ 缩略图仓储/服务的呈现器（uuid 唯一对外键）。

契约（P1 收敛）：
    · set_assets 收 uuid 列表；两个对外信号也吐 uuid。
    · 内部 `_asset_uuids` 是唯一索引；`assets.id` 不进入本模块。
    · 依赖注入接口：
          thumb_repo.get_paths_by_uuid(uuid) -> ThumbnailPaths | None
          thumb_service.submit_by_uuid(uuid, *, include_deleted=False)
              -> concurrent.futures.Future | None
          thumb_service.thumbnail_ready  # 自定义 Signal，广播 asset_uuid

循环防御（P1）：
    · 加载线程区分「加载异常」与「图不存在」：
        - 异常       → emit(item_failed, reason)，**不触发生成**；
        - 图不存在   → 才走 _submit_generation。
      否则一旦加载链路上出现持久性错误（签名变动 / 权限 / 解码失败），
      生成成功 → 广播回来 → 再次加载失败 会形成无限循环。
    · `_attempts` 只在 submit 成功（result.ok）时清零；
      `_on_thumbnail_ready` 不清 zero，避免广播路径把重试上限架空。
    · 连续失败 ≥ _MAX_ATTEMPTS 后走 item_failed("max_attempts")，循环终止。

其余职责 / 线程模型 / 展示约定与旧版相同：
    - 磁盘 I/O + QImage 加载 + 方形裁剪跑在独立线程池；
    - 生成任务交给 ThumbnailService；
    - set_pixmap / 信号槽回调都在主线程（QueuedConnection）。

对外信号：
    item_loaded(str)          —— index 已成功写入 pixmap（载荷 uuid）
    item_failed(str, str)     —— 载荷 (uuid, reason)
    item_activated(str)       —— 载荷 uuid

用法::

    presenter = ThumbnailGridPresenter(
        view=grid, thumb_repo=thumb_repo, thumb_service=thumb_service,
        spec="small",
    )
    presenter.set_assets([str(u1), str(u2), ...])
"""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Optional

from PySide6.QtCore import QObject, QRect, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QImage, QPixmap

from albuswall.common.enums import ThumbSpec
from albuswall.log import Logger, getLogger

from ..delegate.virtual_scroll_info_delegate import InfoCellDelegate, InfoMode
from ..widgets.virtual_scroll import VirtualScrollWidget

__all__ = ["ThumbnailGridPresenter"]

_MAX_ATTEMPTS = 3
_logger: Logger = getLogger(__name__)  # type: ignore


def _coerce_spec(spec: ThumbSpec | str) -> ThumbSpec:
    """规格归一：接受 ``ThumbSpec`` 或其字符串值，其它值直接报错。"""
    try:
        return ThumbSpec(spec)
    except ValueError as exc:
        raise ValueError(f"unsupported spec: {spec!r}") from exc


def _coerce_icon(src: QIcon | QPixmap | str | None) -> QIcon | None:
    """路径 / QPixmap / QIcon → QIcon；空值返回 None。"""
    if src is None:
        return None
    if isinstance(src, QIcon):
        return src if not src.isNull() else None
    if isinstance(src, QPixmap):
        return None if src.isNull() else QIcon(src)
    # 字符串：既支持 ":/..." 资源，也支持普通文件路径
    icon = QIcon(str(src))
    return icon if not icon.isNull() else None


class ThumbnailGridPresenter(QObject):
    """把缩略图按需渲染到 ``VirtualScrollWidget`` 上。"""

    item_loaded = Signal(str)  # asset_uuid
    item_failed = Signal(str, str)  # (asset_uuid, reason)
    item_activated = Signal(str)  # asset_uuid

    # 跨线程投递：
    #   (index, generation, QImage|None, error_str)
    #   error_str 非空 ⇒ 加载侧异常；image 为 None 且 error 为空 ⇒ 图不存在
    _load_result = Signal(int, int, object, str)
    _gen_done = Signal(str)  # asset_uuid
    _thumb_ready = Signal(str)  # asset_uuid

    #: 居中偏好，(x, y) ∈ [0, 1]。
    _CROP_CENTER: tuple[float, float] = (0.5, 0.5)

    def __init__(
            self,
            view: VirtualScrollWidget,
            thumb_repo: Any,
            thumb_service: Optional[Any] = None,
            *,
            spec: ThumbSpec = ThumbSpec.SMALL,
            max_workers: int = 4,
            parent: Optional[QObject] = None,
            view_repo: Optional[Any] = None,  # ← 读 is_favorite / deleted_at
            trash_retention_days: int = 30,  # ← 回收站保留天数
    ) -> None:
        super().__init__(parent)

        self._view = view
        self._repo = thumb_repo
        self._service = thumb_service
        self._spec = _coerce_spec(spec)
        self._view_repo = view_repo
        self._trash_retention_days = max(0, int(trash_retention_days))

        # 每个 asset 的生成尝试计数；只在 submit 成功时清零，
        # 由 _MAX_ATTEMPTS 兜底终止循环。
        self._attempts: dict[str, int] = {}

        # 按 uuid 缓存 (is_favorite, days)，set_assets 时整体丢弃。
        self._info_cache: dict[str, tuple[Optional[bool], Optional[int]]] = {}

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="thumb-load",
        )

        # 数据源
        self._asset_uuids: list[str] = []
        self._generation = 0
        self._include_deleted: bool = False

        # 去抖
        self._inflight: set[str] = set()
        self._submitted: set[str] = set()
        self._visible_range: tuple[int, int] = (0, -1)

        # 跨线程回调切到主线程
        self._load_result.connect(
            self._on_load_result, Qt.ConnectionType.QueuedConnection
        )
        self._gen_done.connect(
            self._on_generation_done, Qt.ConnectionType.QueuedConnection
        )

        # signal 桥（支持 QueuedConnection）
        self._thumb_ready.connect(
            self._on_thumbnail_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        if self._service is not None and hasattr(
                self._service, "thumbnail_ready"
        ):
            # 契约：cb(sender: ThumbnailService, *, asset_uuid: str)
            self._service.thumbnail_ready.connect(
                lambda _sender, *, asset_uuid: self._thumb_ready.emit(asset_uuid)
            )

        view.visible_range_changed.connect(self._on_visible_range_changed)
        view.cache_cleared.connect(self._on_cache_cleared)
        view.unit_clicked.connect(self._on_unit_clicked)
        delegate = InfoCellDelegate(
            pixmap_getter=lambda i: self._view.pixmap_cache.get(i),
            mode=InfoMode.FAVORITE,
            favorite_getter=self._favorite_at_index,
            days_getter=self._days_at_index,
        )
        self._view.delegate = delegate

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def setup(self, _):
        ...

    def teardown(self):
        """释放线程池。幂等。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def set_assets(
            self,
            asset_uuids: list[str],
            *,
            include_deleted: bool = False,
    ) -> None:
        uuids = [str(u) for u in asset_uuids]
        _logger.info(
            "set_assets called: n=%d, include_deleted=%s",
            len(uuids), include_deleted,
        )
        self._generation += 1
        self._asset_uuids = uuids
        self._include_deleted = include_deleted
        self._inflight.clear()
        self._submitted.clear()
        self._visible_range = (0, -1)

        # 数据源换了 → 信息缓存整体丢弃
        self._info_cache.clear()

        # 按 scope 切换信息条模式：回收站→天数，其它→收藏
        self._apply_info_mode_for_scope(include_deleted)

        self._view.reset_source(len(self._asset_uuids) - 1)
        self._attempts.clear()

    def set_spec(self, spec: ThumbSpec | str) -> None:
        """切换缩略图规格；清空缓存后按需重载。"""
        spec = _coerce_spec(spec)
        if spec == self._spec:
            return
        self._spec = spec
        self._inflight.clear()
        self._view.clear_cache()
        self._view.refresh_visible_range()

    def retranslate(self) -> None:
        """语言切换后重绘网格（信息条上的倒计时文案在 paint 时取译文）。"""
        self._view.update()

    def set_favorite_icon(
            self,
            icon: QIcon | QPixmap | str | None,
            *,
            checked: bool = True,
    ) -> None:
        """设置"收藏 / 未收藏"状态图标。

        Args:
            icon:    ``QIcon`` / ``QPixmap`` / 文件路径 / 资源路径（``":/..."``）；
                     传 ``None`` 清除该状态图标（回退到文本，如 ``"★"``）。
            checked: ``True`` 设置"已收藏"图标；``False`` 设置"未收藏"图标。
        """
        d = self._view.delegate
        if not isinstance(d, InfoCellDelegate):
            _logger.warning(
                "Delegate is not an InfoCellDelegate; icon setup ignored"
            )
            return
        d.set_icon(_coerce_icon(icon), checked=checked)
        self._view.update()

    def set_favorite_icons(
            self,
            *,
            on: QIcon | QPixmap | str | None = None,
            off: QIcon | QPixmap | str | None = None,
    ) -> None:
        """一次性设置两个状态图标（``on``=已收藏，``off``=未收藏）。"""
        self.set_favorite_icon(on, checked=True)
        self.set_favorite_icon(off, checked=False)

    # ------------------------------------------------------------------ #
    # 槽：控件事件
    # ------------------------------------------------------------------ #
    @Slot(int, int, int)
    def _on_visible_range_changed(
            self, start: int, end: int, request_id: int
    ) -> None:
        _logger.trace(
            "visible range: %d-%d (n=%d)",
            start, end, len(self._asset_uuids),
        )
        self._visible_range = (start, end)
        if not self._asset_uuids:
            return

        n = len(self._asset_uuids)
        for index in range(start, end + 1):
            if 0 <= index < n and not self._view.has_pixmap(index):
                self._start_load(index)

    @Slot()
    def _on_cache_cleared(self) -> None:
        self._inflight.clear()

    @Slot(int)
    def _on_unit_clicked(self, index: int) -> None:
        if 0 <= index < len(self._asset_uuids):
            self.item_activated.emit(self._asset_uuids[index])

    # ------------------------------------------------------------------ #
    # 槽：加载/生成结果（主线程）
    # ------------------------------------------------------------------ #
    @Slot(int, int, object, str)
    def _on_load_result(
            self, index: int, generation: int, image: object, error: str
    ) -> None:
        if generation != self._generation:
            return  # 数据源已更换，丢弃过期结果
        if not 0 <= index < len(self._asset_uuids):
            return

        asset_uuid = self._asset_uuids[index]
        self._inflight.discard(asset_uuid)

        if isinstance(image, QImage) and not image.isNull():
            self._view.set_pixmap(index, QPixmap.fromImage(image))
            self.item_loaded.emit(asset_uuid)
            return

        if error:
            # 加载侧异常（resolve 失败 / QImage 解码失败 / 权限 …）：
            # 直接上报，不再触发生成 —— 否则会形成
            # “生成成功 → 广播回来 → 再次加载失败” 的无限循环。
            _logger.debug(
                "load failed asset=%s reason=%s (no regen)", asset_uuid, error,
            )
            self.item_failed.emit(asset_uuid, error)
            return

        # 真正“磁盘上没有” → 交给生成服务
        self._submit_generation(asset_uuid, index)

    @Slot(str)
    def _on_generation_done(self, asset_uuid: str) -> None:
        self._submitted.discard(asset_uuid)

        try:
            index = self._asset_uuids.index(asset_uuid)
        except ValueError:
            return  # 已不在当前数据源里

        start, end = self._visible_range
        if start <= index <= end and not self._view.has_pixmap(index):
            self._start_load(index)

    @Slot(str)
    def _on_thumbnail_ready(self, asset_uuid: str) -> None:
        """主线程执行：后台（backfill）缩略图就绪后重载。

        契约：
            · 本方法**不管理** `_submitted` / `_attempts` —— 那两项只由
              本模块自己发起的提交路径（`_submit_generation` /
              `_on_submit_finished`）维护。
            · 广播可能来自第三方 backfill，与本模块的计数语义无关；
              越权重置会让 `_MAX_ATTEMPTS` 形同虚设，进而掩盖循环。
            · 只负责「发现变可用 → 重载一次」，重载失败由
              `_on_load_result` 的 error 分支处理。
        """
        try:
            index = self._asset_uuids.index(asset_uuid)
        except ValueError:
            return  # 已经不在当前数据源里
        if not self._view.has_pixmap(index):
            self._start_load(index)

    # ------------------------------------------------------------------ #
    # 内部：信息条（is_favorite / 剩余天数，读 view_repo）
    # ------------------------------------------------------------------ #
    def _uuid_at_index(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._asset_uuids):
            return self._asset_uuids[index]
        return None

    def _favorite_at_index(self, index: int) -> Optional[bool]:
        uuid = self._uuid_at_index(index)
        if uuid is None:
            return None
        return self._info_for_uuid(uuid)[0]

    def _days_at_index(self, index: int) -> Optional[int]:
        uuid = self._uuid_at_index(index)
        if uuid is None:
            return None
        return self._info_for_uuid(uuid)[1]

    def _info_for_uuid(
            self, asset_uuid: str
    ) -> tuple[Optional[bool], Optional[int]]:
        """返回 ``(is_favorite, remaining_days)``；缺失项为 ``None``。

        每 uuid 只查一次 repo，paint 期间命中缓存。
        """
        cached = self._info_cache.get(asset_uuid)
        if cached is not None:
            return cached
        info = self._fetch_info(asset_uuid)
        self._info_cache[asset_uuid] = info
        return info

    def _fetch_info(
            self, asset_uuid: str
    ) -> tuple[Optional[bool], Optional[int]]:
        if self._view_repo is None:
            return None, None
        try:
            dto = self._view_repo.get_asset(asset_uuid, include_deleted=True)
        except Exception:
            _logger.exception(
                "fetch asset info failed uuid=%s", asset_uuid,
            )
            return None, None
        if dto is None:
            return None, None

        fav_val = getattr(dto, "is_favorite", None)
        fav: Optional[bool] = None if fav_val is None else bool(fav_val)

        days = self._remaining_days(getattr(dto, "deleted_at", None))
        return fav, days

    def _remaining_days(self, deleted_at: Any) -> Optional[int]:
        """``deleted_at`` → 距回收站保留期到期的天数。

        - ``deleted_at`` 为 ``None``（不在回收站） → ``None``；
        - 已过期                                  → ``0``。
        """
        if deleted_at is None:
            return None
        try:
            dt = datetime.fromisoformat(str(deleted_at))
        except (TypeError, ValueError):
            _logger.debug("bad deleted_at: %r", deleted_at)
            return None
        if dt.tzinfo is None:
            # SQLite strftime('%Y-%m-%dT%H:%M:%f','now') 产出的是 UTC 裸时间
            dt = dt.replace(tzinfo=UTC)

        elapsed_days = (
                               datetime.now(UTC) - dt
                       ).total_seconds() / 86400.0
        remaining = self._trash_retention_days - elapsed_days
        return max(0, int(remaining))

    # ------------------------------------------------------------------ #
    # 内部：加载
    # ------------------------------------------------------------------ #
    def _apply_info_mode_for_scope(self, include_deleted: bool) -> None:
        """按当前视图 scope 切换信息条显示模式。

        - Trash（include_deleted=True） → ``InfoMode.DAYS``
        - 其它（Active 视图）           → ``InfoMode.FAVORITE``
        """
        d = self._view.delegate
        if not isinstance(d, InfoCellDelegate):
            return
        d.mode = InfoMode.DAYS if include_deleted else InfoMode.FAVORITE
        self._view.update()

    def _start_load(self, index: int) -> None:
        asset_uuid = self._asset_uuids[index]
        if asset_uuid in self._inflight:
            return
        self._inflight.add(asset_uuid)
        gen = self._generation
        spec = self._spec  # 快照，避免 set_spec 竞争
        self._executor.submit(
            self._load_worker, index, asset_uuid, gen, spec,
        )

    def _load_worker(
            self, index: int, asset_uuid: str, generation: int, spec: ThumbSpec
    ) -> None:
        image: Optional[QImage] = None
        error: str = ""
        try:
            image = self._try_load_image(asset_uuid, spec)
        except Exception as exc:  # worker 顶层兜底
            error = f"load:{type(exc).__name__}"
            _logger.error(
                "thumbnail load error asset=%s: %s", asset_uuid, exc,
            )
        # 通过信号回到主线程；error 非空表示加载侧异常
        self._load_result.emit(index, generation, image, error)

    def _try_load_image(
            self, asset_uuid: str, spec: ThumbSpec
    ) -> Optional[QImage]:
        """返回 QImage 或 None。

        - None    —— 磁盘上没有该 spec 的缩略图（可安全触发生成）；
        - 抛异常 —— 加载侧异常（resolve / 文件系统 / 解码等问题），
                    调用方（_load_worker）捕获后会带 error 上报，
                    主线程据此走 item_failed，不再触发生成。
        """
        paths = self._repo.get_paths_by_uuid(asset_uuid)
        if paths is None:
            return None

        # ThumbnailPaths 自带 resolve：base + spec 组合由它单点负责。
        full = paths.resolve(spec)
        if not full:
            return None
        if not os.path.isfile(full):
            return None

        image = QImage(full)
        if image.isNull():
            return None

        return self._center_crop_square(image)

    @classmethod
    def _center_crop_square(cls, image: QImage) -> QImage:
        """把 QImage 按 ``_CROP_CENTER`` 居中裁成方形（纯裁剪不缩放）。"""
        w, h = image.width(), image.height()
        if w == h:
            return image

        side = min(w, h)
        cx, cy = cls._CROP_CENTER
        x = int(round((w - side) * cx))
        y = int(round((h - side) * cy))
        x = max(0, min(x, w - side))
        y = max(0, min(y, h - side))

        return image.copy(QRect(x, y, side, side))

    # ------------------------------------------------------------------ #
    # 内部：生成
    # ------------------------------------------------------------------ #
    def _submit_generation(self, asset_uuid: str, index: int) -> None:
        if self._service is None:
            self.item_failed.emit(asset_uuid, "no_thumbnail")
            return
        if asset_uuid in self._submitted:
            return

        n = self._attempts.get(asset_uuid, 0)
        if n >= _MAX_ATTEMPTS:
            _logger.warning(
                "thumbnail generation hit max attempts asset=%s, giving up",
                asset_uuid,
            )
            self.item_failed.emit(asset_uuid, "max_attempts")
            return
        self._attempts[asset_uuid] = n + 1

        try:
            future: Future | None = self._service.submit_by_uuid(
                asset_uuid,
                include_deleted=self._include_deleted,
            )
        except Exception as exc:
            _logger.error(
                "submit thumbnail failed asset=%s: %s", asset_uuid, exc,
            )
            self.item_failed.emit(
                asset_uuid, f"submit:{type(exc).__name__}"
            )
            return

        # submit 命中 inflight 去重时返回 None；
        # 该任务的完成会经 ThumbnailService.thumbnail_ready 广播回来。
        if future is None:
            _logger.trace(
                "thumbnail submit deduped asset=%s, waiting for broadcast",
                asset_uuid,
            )
            self._submitted.add(asset_uuid)
            return

        self._submitted.add(asset_uuid)
        future.add_done_callback(
            lambda fut, au=asset_uuid: self._on_submit_finished(au, fut)
        )

    def _on_submit_finished(self, asset_uuid: str, future: Future) -> None:
        try:
            result = future.result()
            if getattr(result, "ok", False):
                # 唯一合法清零点：本次生成确实成功。
                self._attempts.pop(asset_uuid, None)
            else:
                error = getattr(result, "error", "?")
                retryable = bool(getattr(result, "retryable", True))
                _logger.warning(
                    "thumbnail generation failed asset=%s: %s (retryable=%s)",
                    asset_uuid, error, retryable,
                )
                if not retryable:
                    return
        except Exception as exc:
            _logger.error(
                "thumbnail future error asset=%s: %s", asset_uuid, exc,
            )

        self._gen_done.emit(asset_uuid)
