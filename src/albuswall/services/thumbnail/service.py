#
"""缩略图编排者（uuid 唯一对外键）。

契约（P1 收敛）：
    · 所有公开方法的 asset 键是 **uuid**；``assets.id`` 只在 worker 内部
      由 ``get_task_input_by_uuid`` 一次性取出，用于 ``update_paths`` 回写。
    · ``thumbnail_ready`` 广播 uuid。
    · ``_inflight`` / ``_failed`` 的键都是 uuid。

职责
----
- 提交任务到 TaskService（不做自己的队列/并发/重试）
- 去重：同一 (asset_uuid, version) 不重复入队
- 失败分类：永久性失败不进重试；暂时性失败允许重试
- 冷却：永久失败的资产在 cooldown 窗口内不再被 scan_and_submit 提交
- 编排：查上下文 → 渲染 → 写盘 → 回填路径
- purge：删文件 + 清库

配置来源
--------
从 ``services.thumbnail.config.config`` 单例上直接取。
需要归一化的字段（thumb_root / specs / image_format）在
``__init__`` 里调用 config 模块的 resolve_* 帮助函数，一次性缓存。

路径契约
--------
写：thumb_path 列 ← **绝对** base 目录；thumb_<spec>_path 列 ← 相对文件名。
读：只从 DB 取 (thumb_path, spec_rel) 直接拼，不重算 base ——
    version 升级后旧资产仍指向旧版本目录，读到实际存在的文件。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, wait as futures_wait
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING
from uuid import UUID

from albuswall.dto.task import ExecutorType
from albuswall.dto.thumbnail import (
    ALL_SPECS,
    ThumbnailResult,
    ThumbnailStats,
    ThumbSpec,
)
from albuswall.log import getLogger
from albuswall.utils import path as path_util
from albuswall.utils.signals import Signal

from .config import (
    config,
    resolve_thumb_root,
    resolve_specs,
    resolve_image_format,
)
from .paths import build_thumb_paths
from .renderer import is_permanent_render_error, render
from .storage import ThumbnailFormatError, ThumbnailStorage

if TYPE_CHECKING:
    from albuswall.repositories import ThumbnailRepository
    from albuswall.repositories.source import IngestSourceRepository
    from albuswall.infrastructure.task.protocol import TaskServiceProtocol

_logger = getLogger(__name__)

PRIORITY_AFTER_IMPORT = 0
PRIORITY_MANUAL = 1
PRIORITY_BACKFILL = 5


@dataclass
class _FailureRecord:
    """永久性失败的记录，用于冷却。"""
    failed_at: float


class ThumbnailService:
    """缩略图编排。

    构造时接收：
      - task_service: TaskServiceProtocol（通用任务底座）
      - thumb_repo:   ThumbnailRepository（落库）
      - source_repo:  IngestSourceRepository（补 source_path）

    配置从 ``config`` 单例上取，不走构造参数。
    """
    thumbnail_ready: Signal = Signal(name="ThumbnailReady")

    def __init__(
            self,
            task_service: "TaskServiceProtocol",
            thumb_repo: "ThumbnailRepository",
            source_repo: "IngestSourceRepository",
    ):
        self._task_service = task_service
        self._repo = thumb_repo
        self._source_repo = source_repo

        # 从 config 单例上取字段；需要归一化的在这里一次性缓存
        self._version = str(config.version)
        self._thumb_root = resolve_thumb_root()
        self._specs = resolve_specs()
        self._image_format = resolve_image_format()
        self._batch_size = int(config.batch_size)
        self._failure_cooldown_sec = float(config.failure_cooldown_sec)

        self._storage = ThumbnailStorage(
            thumb_root=self._thumb_root,
            fmt=self._image_format,
            quality=int(config.quality),
        )

        self._stopped = False

        # 在途任务：(asset_uuid, version) → Future
        self._inflight: dict[tuple[str, str], Optional[Future]] = {}
        self._inflight_lock = threading.Lock()

        # 永久失败冷却：asset_uuid → 失败时间
        self._failed: dict[str, _FailureRecord] = {}
        self._failed_lock = threading.Lock()

        self._write_max_retries = max(0, int(config.write_max_retries))
        self._write_retry_backoff_sec = max(
            0.0, float(config.write_retry_backoff_sec),
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        _logger.info(
            "ThumbnailService started (version=%s, specs=%s, root=%s)",
            self._version, tuple(self._specs), self._thumb_root,
        )

    def stop(self, wait: bool = True, timeout: Optional[float] = None) -> None:
        """标记停止，可选等待在途任务结束。"""
        self._stopped = True
        if not wait:
            _logger.info("ThumbnailService stopped (no wait)")
            return

        with self._inflight_lock:
            futures = [f for f in self._inflight.values() if f is not None]

        if futures:
            futures_wait(futures, timeout=timeout)
        _logger.info(
            "ThumbnailService stopped (waited on %d futures)", len(futures),
        )

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def submit_by_uuid(
            self,
            asset_uuid: str,
            *,
            include_deleted: bool = False,
            priority: int = PRIORITY_AFTER_IMPORT,
            executor: ExecutorType = ExecutorType.THREAD,
    ) -> Optional[Future]:
        """提交单个资产（uuid）。同一 (uuid, version) 在途时返回 None。"""
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        if executor != "thread":
            raise ValueError(...)

        # 去重键 (uuid, version)：删/未删决定的是“能不能查到”，
        # 不影响生成的缩略图内容，所以不需要把 include_deleted 塞进 key。
        key = (asset_uuid, self._version)
        with self._inflight_lock:
            if key in self._inflight:
                _logger.trace(...)
                return None
            self._inflight[key] = None

        try:
            future = self._task_service.submit(
                self._work_one, asset_uuid, include_deleted,
                executor=ExecutorType.THREAD, priority=priority,
            )
        except Exception:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            raise

        with self._inflight_lock:
            if key in self._inflight:
                self._inflight[key] = future

        future.add_done_callback(self._make_release_cb(asset_uuid, key))
        return future

    def submit_bulk_by_uuid(
            self,
            asset_uuids: Sequence[str],
            *,
            include_deleted: bool = False,
            priority: int = PRIORITY_AFTER_IMPORT,
            executor: ExecutorType = ExecutorType.THREAD,
    ) -> list[Future]:
        """批量提交（uuid）。返回实际入队的 future 列表。"""
        futures: list[Future] = []
        for u in asset_uuids:
            fut = self.submit_by_uuid(
                u,
                include_deleted=include_deleted,
                priority=priority,
                executor=executor,
            )
            if fut is not None:
                futures.append(fut)
        return futures

    def _make_release_cb(self, asset_uuid: str, key: tuple[str, str]):
        """任务完成回调：释放 in-flight 标记，按结果决定是否记冷却。"""

        def _cb(fut: Future) -> None:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            try:
                result: ThumbnailResult = fut.result()
            except Exception:
                return
            if result.ok:
                with self._failed_lock:
                    self._failed.pop(asset_uuid, None)
            elif not result.retryable:
                with self._failed_lock:
                    self._failed[asset_uuid] = _FailureRecord(
                        failed_at=time.monotonic(),
                    )

        return _cb

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def regenerate(self, uuid: str) -> Optional[Future]:
        """手动重建：按 uuid 提交，优先级 MANUAL。"""
        task_input = self._repo.get_task_input_by_uuid(uuid)
        if task_input is None:
            raise KeyError(f"asset not found by uuid: {uuid!r}")
        return self.submit_by_uuid(uuid, priority=PRIORITY_MANUAL)

    def scan_and_submit(self, batch: Optional[int] = None) -> int:
        """扫一遍缺缩略图的活跃资产，提交补齐。返回实际提交数。

        永久失败且处于冷却期的资产会被跳过（防止死循环提交）。
        """
        limit = batch or self._batch_size
        rows = self._repo.list_missing(specs=ALL_SPECS, limit=limit)
        if not rows:
            return 0

        eligible = [r.uuid for r in rows if not self._is_in_cooldown(r.uuid)]
        skipped = len(rows) - len(eligible)
        if skipped:
            _logger.info(
                "thumbnail backfill: %d assets in cooldown, skipped", skipped,
            )
        if not eligible:
            return 0

        _logger.info("thumbnail backfill: submitting %d tasks", len(eligible))
        self.submit_bulk_by_uuid(eligible, priority=PRIORITY_BACKFILL)
        return len(eligible)

    def run_once(self, batch: Optional[int] = None) -> dict:
        """同步跑一轮：扫描 → 提交 → 等结果。主要用于测试 / 手动触发。"""
        limit = batch or self._batch_size
        rows = self._repo.list_missing(specs=ALL_SPECS, limit=limit)
        if not rows:
            return {"scanned": 0, "ok": 0, "failed": 0, "skipped": 0}

        futures = self.submit_bulk_by_uuid(
            [r.uuid for r in rows], priority=PRIORITY_BACKFILL,
        )
        skipped = len(rows) - len(futures)

        ok = failed = 0
        for fut in futures:
            # noinspection broad-exception
            try:
                if fut.result().ok:
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {
            "scanned": len(rows), "ok": ok,
            "failed": failed, "skipped": skipped,
        }

    def stats(self) -> ThumbnailStats:
        return self._repo.count_by_status()

    def _is_in_cooldown(self, asset_uuid: str) -> bool:
        with self._failed_lock:
            rec = self._failed.get(asset_uuid)
        if rec is None:
            return False
        return (time.monotonic() - rec.failed_at) < self._failure_cooldown_sec

    # ------------------------------------------------------------------ #
    # 读取：只走 DB，禁止重算路径
    # ------------------------------------------------------------------ #
    def get_thumbnail_path(
            self,
            asset_uuid: str,
            spec: ThumbSpec = ThumbSpec.MEDIUM,
    ) -> Optional[str]:
        """获取某个 spec 缩略图的绝对路径；未生成或资产不存在返回 None。

        **不过滤 is_deleted**：回收站预览用得上。
        """
        paths = self._repo.get_paths_by_uuid(asset_uuid)
        if paths is None:
            return None
        return paths.resolve(spec)

    def get_thumbnail_paths(
            self,
            asset_uuids: Sequence[str],
            spec: ThumbSpec = ThumbSpec.MEDIUM,
    ) -> dict[str, str]:
        """批量版；返回 {asset_uuid: abs_path}。已删除资产也会被读出。"""
        if not asset_uuids:
            return {}
        rows = self._repo.get_paths_bulk_by_uuid(asset_uuids)
        result: dict[str, str] = {}
        for u, paths in rows.items():
            p = paths.resolve(spec)
            if p:
                result[u] = p
        return result

    # ------------------------------------------------------------------ #
    # 永久删除：先删文件，再清库
    # ------------------------------------------------------------------ #
    def purge(self, asset_uuid: str) -> bool:
        """永久删除单个资产的所有缩略图（uuid）。

        **仅限永久删除（清空回收站）**。软删（is_deleted=1）不要调这里 ——
        软删必须保留缩略图供 Trash 预览。

        返回 True 表示资产存在并已处理；False 表示资产不存在。
        """
        paths = self._repo.get_paths_by_uuid(asset_uuid)
        if paths is None:
            return False
        self._storage.delete(paths)
        self._repo.clear_and_snapshot_by_uuid(asset_uuid)
        _logger.info("thumbnail purged uuid=%s", asset_uuid)
        return True

    def purge_bulk(self, asset_uuids: Sequence[str]) -> int:
        """批量 purge（uuid）；返回处理的资产数。用于 Trash 批量清空。"""
        if not asset_uuids:
            return 0
        rows = self._repo.get_paths_bulk_by_uuid(asset_uuids)
        if not rows:
            return 0
        for paths in rows.values():
            self._storage.delete(paths)
        self._repo.clear_and_snapshot_bulk_by_uuid(list(rows.keys()))
        _logger.info("thumbnail purged: %d assets", len(rows))
        return len(rows)

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _work_one(
            self, asset_uuid: str,
            include_deleted: bool = False
    ) -> ThumbnailResult:
        """TaskService 调用的入口（uuid）。

        去重标记由 submit_by_uuid() 挂在 future 上的 done callback 释放。
        """
        started = time.monotonic()

        # 1. 取上下文（默认过滤 is_deleted=0；Trash 视图传 True 放行）
        task_input = self._repo.get_task_input_by_uuid(
            asset_uuid, include_deleted=include_deleted,
        )
        if task_input is None:
            # 到这里才区分：是「被过滤掉的已删资产」还是「根本不存在」
            if self._repo.get_paths_by_uuid(asset_uuid) is not None:
                _logger.info(
                    "thumbnail skipped: asset %s is deleted", asset_uuid,
                )
                return ThumbnailResult(
                    ok=False, asset_uuid=UUID(asset_uuid),
                    error="asset_deleted_filtered", retryable=False,
                )
            _logger.warning(
                "thumbnail skipped: asset %s not found", asset_uuid,
            )
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error="asset_not_found", retryable=False,
            )

        # 2. 定位源文件
        src = path_util.resolve_asset_source(
            source_id=task_input.source_id,
            source_path=task_input.source_path,
            file_path=task_input.file_path,
        )
        if not src.is_file():
            _logger.warning("thumbnail skipped: source missing %s", src)
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error="source_missing", retryable=False,
            )

        # 3. 渲染（纯计算，不碰磁盘）
        try:
            imgs = render(src, self._specs)
        except Exception as exc:
            if is_permanent_render_error(exc):
                _logger.warning(
                    "thumbnail render rejected asset=%s (%s): %s",
                    asset_uuid, type(exc).__name__, exc,
                )
                return ThumbnailResult(
                    ok=False, asset_uuid=UUID(asset_uuid),
                    error=f"render:{type(exc).__name__}", retryable=False,
                )
            _logger.error(
                "thumbnail render failed asset=%s: %s", asset_uuid, exc,
            )
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error=f"render:{type(exc).__name__}", retryable=True,
            )

        # 4. 写盘（带有限重试；超出上限或格式不兼容 → 永久失败）
        base_rel, spec_rels = build_thumb_paths(
            uuid=task_input.uuid,
            version=self._version,
            fmt=self._image_format,
        )
        base_abs = None
        try:
            base_abs = self._write_with_retry(
                asset_uuid, base_rel, spec_rels, imgs,
            )
        except ThumbnailFormatError as exc:
            _logger.error(
                "thumbnail write rejected asset=%s (format): %s",
                asset_uuid, exc,
            )
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error=f"write_format:{type(exc).__name__}",
                retryable=False,
            )
        except Exception as exc:
            _logger.error(
                "thumbnail write failed asset=%s after %d retries: %s",
                asset_uuid, self._write_max_retries, exc,
            )
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error=f"write:{type(exc).__name__}",
                retryable=False,
            )
        finally:
            for img in imgs.values():
                # noinspection broad-exception
                try:
                    img.close()
                except Exception:
                    pass

        # 5. 回填路径（base 绝对，spec 相对）—— id 只在这一次内部写回
        self._repo.update_paths_by_uuid(
            asset_uuid,
            paths=spec_rels,
            base_dir=base_abs,
        )

        # 广播（uuid）：通知 UI 等订阅方可以重新读了。
        try:
            self.thumbnail_ready.emit(asset_uuid)
        except Exception:
            _logger.exception(
                "emit thumbnail_ready failed asset=%s", asset_uuid,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        _logger.trace(
            "thumbnail done uuid=%s in %dms", task_input.uuid, duration_ms,
        )
        return ThumbnailResult(
            ok=True, asset_uuid=UUID(asset_uuid), duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------ #
    # 写盘：有限重试
    # ------------------------------------------------------------------ #
    def _write_with_retry(
            self,
            asset_uuid: str,
            base_rel: str,
            spec_rels: dict,
            imgs: dict,
    ) -> str:
        """带有限重试的写盘。首次 + write_max_retries 次重试，指数退避。"""
        attempts = self._write_max_retries + 1
        last_exc: Optional[BaseException] = None

        for i in range(attempts):
            try:
                return self._storage.write(base_rel, spec_rels, imgs)
            except ThumbnailFormatError:
                raise
            except OSError as exc:
                last_exc = exc
                if i + 1 >= attempts:
                    break
                backoff = self._write_retry_backoff_sec * (2 ** i)
                _logger.debug(
                    "thumbnail write retry %d/%d asset=%s: %s",
                    i + 1, self._write_max_retries, asset_uuid, exc,
                )
                if backoff > 0:
                    time.sleep(backoff)

        assert last_exc is not None
        raise last_exc
