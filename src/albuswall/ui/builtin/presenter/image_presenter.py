#
"""虚拟滚动网格 ⇄ 缩略图仓储/服务的呈现器。

职责边界：
    * 只通过 ``VirtualScrollWidget`` 的公开 API 与之交互
      (``set_pixmap`` / ``has_pixmap`` / ``reset_source`` / ``clear_cache`` /
       ``refresh_visible_range`` / 两个信号)。
    * 不关心缩略图从哪来 —— 依赖注入 ``thumb_repo`` 与可选的 ``thumb_service``。
      约定接口（协议级，无需继承）：
          thumb_repo.get_paths(asset_id) -> object | None
              返回对象需具备 base / small / medium / large 四个属性
              （即 ThumbnailPaths 的形状）。
          thumb_repo.resolve_path(base, spec_path) -> str | None
          thumb_service.submit(asset_id, *, include_deleted: bool = False)
                -> concurrent.futures.Future
              future.result() 需具备 .ok: bool 属性。

展示约定：
    * 网格 cell 要求方形图像，而磁盘上的缩略图保持源图长宽比（无损、通用）。
      方形是 *UI 布局约束*，因此在这里（加载线程内、写 pixmap 之前）做
      居中裁剪；不落盘，需求变化时改这一处即可，缩略图文件不用重跑。

线程模型：
    * 磁盘 I/O + QImage 加载 + 方形裁剪：跑在独立线程池。
    * 生成任务：交给 ThumbnailService 自己的线程池，不阻塞加载线程。
    * 所有 ``set_pixmap`` / 信号槽回调：都在主线程（靠 QueuedConnection 保证）。

对外信号：
    item_loaded(int)              —— index 已成功写入 pixmap
    item_failed(int, str)         —— index 最终无法加载（reason）

用法::

    presenter = ThumbnailGridPresenter(
        view=grid,
        thumb_repo=thumb_repo,
        thumb_service=thumb_service,
        spec="small",
    )
    presenter.set_assets([101, 102, 103, ...])   # 触发首次加载
    ...
    presenter.shutdown()
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

from PySide6.QtCore import QObject, QRect, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from albuswall.dto.thumbnail import ThumbSpec
from albuswall.log import TRACE, Logger

try:
    from ..widgets.virtual_scroll import VirtualScrollWidget
except ImportError:
    from albuswall.ui.builtin.widgets.virtual_scroll import VirtualScrollWidget

__all__ = ["ThumbnailGridPresenter"]

_MAX_ATTEMPTS = 3
_logger: Logger = logging.getLogger(__name__)  # noqa
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


class ThumbnailGridPresenter(QObject):
    """把缩略图按需渲染到 ``VirtualScrollWidget`` 上。"""

    item_loaded = Signal(int)
    item_failed = Signal(int, str)
    item_activated = Signal(int)

    # 跨线程投递（参数用 object 承载 QImage / None）
    _load_result = Signal(int, int, object)  # (index, generation, QImage|None)
    _gen_done = Signal(int)  # asset_id

    #: 居中偏好，(x, y) ∈ [0, 1]。0.5/0.5 = 正中心；
    #: 想“顶部优先”（人像、证件）可改 (0.5, 0.3) 之类。
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
    ) -> None:
        super().__init__(parent)

        if spec not in ("small", "medium", "large"):
            raise ValueError(f"unsupported spec: {spec!r}")

        self._view = view
        self._repo = thumb_repo
        self._service = thumb_service
        self._spec = spec

        self._attempts: dict[int, int] = {}

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="thumb-load",
        )

        # 数据源
        self._asset_ids: list[int] = []
        self._generation = 0
        # 当前视图是否包含已删除资产（由 set_assets 设置）
        self._include_deleted: bool = False

        # 去抖
        self._inflight: set[int] = set()  # 正在磁盘加载的 asset_id
        self._submitted: set[int] = set()  # 已提交生成的 asset_id
        self._visible_range: tuple[int, int] = (0, -1)

        # 跨线程回调切到主线程
        self._load_result.connect(
            self._on_load_result, Qt.ConnectionType.QueuedConnection
        )
        self._gen_done.connect(
            self._on_generation_done, Qt.ConnectionType.QueuedConnection
        )

        view.visible_range_changed.connect(self._on_visible_range_changed)
        view.cache_cleared.connect(self._on_cache_cleared)
        view.unit_clicked.connect(self._on_unit_clicked)

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def set_assets(
            self,
            asset_ids: list[int],
            *,
            include_deleted: bool = False,
    ) -> None:
        """重置数据源并滚回顶部，触发一次可见范围重新计算。

        Args:
            asset_ids: 当前视图里的 asset 整数 id 列表。
            include_deleted:
                当前视图是否为 Trash（scope=deleted）。
                True  —— 缺失缩略图时允许触发生成，生成侧 include_deleted=True。
                False —— Active 视图，生成侧 include_deleted=False。
                默认 False，保持向后兼容。
        """
        _logger.info(
            "set_assets called: n=%d, include_deleted=%s",
            len(asset_ids), include_deleted,
        )
        self._generation += 1
        self._asset_ids = list(asset_ids)
        self._include_deleted = include_deleted
        self._inflight.clear()
        self._submitted.clear()
        self._visible_range = (0, -1)

        self._view.reset_source(len(self._asset_ids) - 1)

        self._attempts.clear()

    def set_spec(self, spec: ThumbSpec) -> None:
        """切换缩略图规格；清空缓存后按需重载。"""
        if spec not in ("small", "medium", "large"):
            raise ValueError(f"unsupported spec: {spec!r}")
        if spec == self._spec:
            return
        self._spec = spec
        self._inflight.clear()
        # clear_cache 会发 cache_cleared；refresh_visible_range 强制重发范围
        self._view.clear_cache()
        self._view.refresh_visible_range()

    def shutdown(self) -> None:
        """释放线程池。幂等。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # 槽：控件事件
    # ------------------------------------------------------------------ #
    @Slot(int, int, int)
    def _on_visible_range_changed(self, start: int, end: int, request_id: int) -> None:
        _logger.trace("visible range: %d-%d (n=%d)", start, end, len(self._asset_ids))
        self._visible_range = (start, end)
        if not self._asset_ids:
            return

        n = len(self._asset_ids)
        for index in range(start, end + 1):
            if 0 <= index < n and not self._view.has_pixmap(index):
                self._start_load(index)

    @Slot()
    def _on_cache_cleared(self) -> None:
        # 缓存没了，正在飞行中的工作也不必阻塞后续重试
        self._inflight.clear()

    @Slot(int)
    def _on_unit_clicked(self, index: int) -> None:
        if 0 <= index < len(self._asset_ids):
            self.item_activated.emit(self._asset_ids[index])

    # ------------------------------------------------------------------ #
    # 槽：加载/生成结果（主线程）
    # ------------------------------------------------------------------ #
    @Slot(int, int, object)
    def _on_load_result(self, index: int, generation: int, image: object) -> None:
        if generation != self._generation:
            return  # 数据源已更换，丢弃过期结果
        if not 0 <= index < len(self._asset_ids):
            return

        asset_id = self._asset_ids[index]
        self._inflight.discard(asset_id)

        if isinstance(image, QImage) and not image.isNull():
            self._view.set_pixmap(index, QPixmap.fromImage(image))
            self.item_loaded.emit(index)
        else:
            # 磁盘上还没有 → 交给生成服务
            self._submit_generation(asset_id, index)

    @Slot(int)
    def _on_generation_done(self, asset_id: int) -> None:
        self._submitted.discard(asset_id)

        try:
            index = self._asset_ids.index(asset_id)
        except ValueError:
            return  # 已经不在当前数据源里

        start, end = self._visible_range
        if start <= index <= end and not self._view.has_pixmap(index):
            self._start_load(index)

    # ------------------------------------------------------------------ #
    # 内部：加载
    # ------------------------------------------------------------------ #
    def _start_load(self, index: int) -> None:
        asset_id = self._asset_ids[index]
        if asset_id in self._inflight:
            return
        self._inflight.add(asset_id)
        gen = self._generation
        spec = self._spec  # 快照，避免 set_spec 竞争
        self._executor.submit(self._load_worker, index, asset_id, gen, spec)

    def _load_worker(
            self, index: int, asset_id: int, generation: int, spec: ThumbSpec
    ) -> None:
        image: Optional[QImage] = None
        try:
            image = self._try_load_image(asset_id, spec)
        except Exception as exc:  # noqa: BLE001 —— worker 顶层兜底
            _logger.error("thumbnail load error asset=%d: %s", asset_id, exc)
        # 通过信号回到主线程
        self._load_result.emit(index, generation, image)

    def _try_load_image(
            self, asset_id: int, spec: ThumbSpec) -> Optional[QImage]:
        paths = self._repo.get_paths(asset_id)
        if paths is None:
            return None

        rel = paths.for_spec(spec)
        base = paths.base
        if not rel:
            return None

        full = self._repo.resolve_path(base, rel)
        if not full or not os.path.isfile(full):
            return None

        image = QImage(full)
        if image.isNull():
            return None

        # 网格 cell 要求方形；裁剪在加载线程里做，只影响展示、不落盘。
        return self._center_crop_square(image)

    @classmethod
    def _center_crop_square(cls, image: QImage) -> QImage:
        """把 QImage 按 ``_CROP_CENTER`` 居中裁成方形。

        * 纯裁剪、不缩放：不引入二次重采样，磁盘缩略图的清晰度原样保留。
        * 已是方形时直接原样返回，避免一次多余的深拷贝。
        * 返回的是 ``QImage.copy(rect)``，深拷贝、跨线程 emit 安全。
        """
        w, h = image.width(), image.height()
        if w == h:
            return image

        side = min(w, h)
        cx, cy = cls._CROP_CENTER
        # 用 round 保证 (0.5,0.5) 时居中对称，(0.5,0.0) 时贴顶
        x = int(round((w - side) * cx))
        y = int(round((h - side) * cy))
        # 夹回合法范围，避免浮点偏好把 rect 顶出图像边界
        x = max(0, min(x, w - side))
        y = max(0, min(y, h - side))

        return image.copy(QRect(x, y, side, side))

    # ------------------------------------------------------------------ #
    # 内部：生成
    # ------------------------------------------------------------------ #
    def _submit_generation(self, asset_id: int, index: int) -> None:
        if self._service is None:
            self.item_failed.emit(index, "no_thumbnail")
            return
        if asset_id in self._submitted:
            return

        n = self._attempts.get(asset_id, 0)
        if n >= _MAX_ATTEMPTS:
            self.item_failed.emit(index, "max_attempts")
            return
        self._attempts[asset_id] = n + 1

        try:
            future: Future = self._service.submit(
                asset_id,
                include_deleted=self._include_deleted,  # ← 关键
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error("submit thumbnail failed asset=%d: %s", asset_id, exc)
            self.item_failed.emit(index, f"submit:{type(exc).__name__}")
            return

        self._submitted.add(asset_id)
        future.add_done_callback(
            lambda fut, aid=asset_id: self._on_submit_finished(aid, fut)
        )

    def _on_submit_finished(self, asset_id: int, future: Future) -> None:
        """在 TaskService 的 worker 线程执行 —— 只发信号，不碰 UI。"""
        try:
            result = future.result()
            if not getattr(result, "ok", False):
                error = getattr(result, "error", "?")
                retryable = bool(getattr(result, "retryable", True))
                _logger.warning(
                    "thumbnail generation failed asset=%d: %s (retryable=%s)",
                    asset_id, error, retryable,
                )
                if not retryable:
                    # 永久性失败：不 emit _gen_done，避免触发无意义重试。
                    # _submitted 保持在集合里，阻止同一 asset 再次入队；
                    # 下次 set_assets 会一并清空。
                    return
        except Exception as exc:  # noqa: BLE001
            _logger.error("thumbnail future error asset=%d: %s", asset_id, exc)

        self._gen_done.emit(asset_id)
