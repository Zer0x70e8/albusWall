# coding: utf-8
# noinspection SpellCheckingInspection
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
- purge：只清 DB

模式（布局同构，仅根目录不同）
------------------------------
两种模式共用同一套磁盘协议：``<root>/<xdg-spec>/<md5(file_uri)>.png``，
强制 PNG，PNG 内嵌 ``Thumb::URI`` / ``Thumb::MTime`` / ``Thumb::Size``，
目录 0700 / 文件 0600，永久失败写 ``<root>/fail/<md5>.png``。

* **私有模式（默认）**：``xdg_enabled=False``，``root = resolve_thumb_root()``
  （``config.thumb_root``，默认 ``~/.cache/albuswall/thumbs``）。
* **XDG 共享模式**：``xdg_enabled=True``，``root = xdg_thumbnails_root()``
  （Linux: ``$XDG_CACHE_HOME``/``~/.cache``，macOS: ``~/Library/Caches``，
  Windows: ``%LOCALAPPDATA%``）。

``version`` / ``image_format`` / ``quality`` 不再参与路径或编码 ——
文件名只由源文件绝对路径决定，格式恒为 PNG。``version`` 字段保留，
但只作为 ``_inflight`` 去重键 ``(uuid, version)`` 的组成部分。

配置来源
--------
构造时取一份 ``thumbnail_config()`` 的 typed dict 快照（默认值来自静态
配置声明），之后只读这份快照：服务生命周期内配置不再变化。
需要归一化的字段（thumb_root / specs / xdg_spec_map）在 ``__init__``
里调用 config 模块的 resolve_* 帮助函数，同样一次性缓存。``image_format``
仍然读一次（保留字段），但不参与任何决策。

路径契约
--------
写：thumb_path 列 ← **绝对** root；thumb_<spec>_path 列 ← ``<spec>/<md5>.png``。
读：只从 DB 取 (thumb_path, spec_rel) 直接拼，不重算 base ——
    两种模式下 base 都恒为共享根，spec_rel 同样满足 ``base/rel`` 拼接契约，
    读取路径零改动。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, wait as futures_wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, TYPE_CHECKING
from uuid import UUID

from albuswall.common.constants.thumbnail import ALL_SPECS
from albuswall.common.enums import ExecutorType, ThumbSpec
from albuswall.dto.thumbnail import (
    ThumbnailResult,
    ThumbnailStats,
)
from albuswall.log import getLogger
from albuswall.utils import path as path_util
from albuswall.utils.signals import SignalDescriptor

from .config import (
    VERSION,
    resolve_image_format,
    resolve_specs,
    resolve_thumb_root,
    resolve_xdg_spec_map,
    thumbnail_config,
)
from .paths import build_thumb_paths
from .renderer import is_permanent_render_error, render
from .storage import (
    ThumbnailFormatError,
    ThumbnailStorage,
)
from albuswall.utils.xdg_thumbnail import (
    is_thumbnail_valid_for,
    xdg_fail_path,
    xdg_thumbnails_root,
)

if TYPE_CHECKING:
    from albuswall.repositories import ThumbnailRepository
    from albuswall.repositories.source import IngestSourceRepository
    from albuswall.infrastructure.task import TaskServiceProtocol

_logger = getLogger(__name__)

PRIORITY_AFTER_IMPORT = 0
PRIORITY_MANUAL = 1
PRIORITY_BACKFILL = 5


@dataclass
class _FailureRecord:
    """永久性失败的记录，用于冷却。"""
    failed_at: float


# noinspection broad-exception,SpellCheckingInspection
class ThumbnailService:
    """缩略图编排。

    构造时接收：
      - task_service: TaskServiceProtocol（通用任务底座）
      - thumb_repo:   ThumbnailRepository（落库）
      - source_repo:  IngestSourceRepository（补 source_path）

    配置从 ``config`` 单例上取，不走构造参数。
    """
    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: ThumbnailService, *, asset_uuid: str)
    thumbnail_ready = SignalDescriptor("thumbnail_ready")

    def __init__(
            self,
            task_service: "TaskServiceProtocol",
            thumb_repo: "ThumbnailRepository",
            source_repo: "IngestSourceRepository",
    ):
        self._task_service = task_service
        self._repo = thumb_repo
        self._source_repo = source_repo

        # 取一份 typed dict 快照；需要归一化的在这里一次性缓存
        conf = thumbnail_config()
        self._version = str(VERSION)
        self._specs = resolve_specs(conf)
        # 保留 image_format 字段（其它模块可能在读），但不再参与任何决策：
        # 两种模式都强制 PNG，见 storage.ThumbnailStorage。
        self._image_format = resolve_image_format(conf)
        self._batch_size = int(conf["batch_size"])
        self._failure_cooldown_sec = float(conf["failure_cooldown_sec"])

        # 模式开关只决定根目录：同构布局下再无第二种磁盘协议。
        self._xdg_enabled = bool(conf["xdg_enabled"])
        self._root: Path = (
            xdg_thumbnails_root() if self._xdg_enabled
            else resolve_thumb_root(conf)
        )
        self._spec_map: dict[ThumbSpec, str] = resolve_xdg_spec_map(conf)
        self._storage = ThumbnailStorage(self._root)

        self._stopped = False

        # 在途任务：(asset_uuid, version) → Future
        self._inflight: dict[tuple[str, str], Optional[Future]] = {}
        self._inflight_lock = threading.Lock()

        # 永久失败冷却：asset_uuid → 失败时间
        self._failed: dict[str, _FailureRecord] = {}
        self._failed_lock = threading.Lock()

        self._write_max_retries = max(0, int(conf["write_max_retries"]))
        self._write_retry_backoff_sec = max(
            0.0, float(conf["write_retry_backoff_sec"]),
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")

        # 布局同构后唯一的模式差异就是根目录：标注 mode 便于对日志排查，
        # 但两种模式跑的是同一套代码路径（PNG + 内嵌元数据 + 0700/0600）。
        mode = "XDG shared" if self._xdg_enabled else "private"
        _logger.info(
            "ThumbnailService started (%s mode, root=%s, specs=%s, dirs=%s)",
            mode, self._root,
            tuple(s.value for s in self._specs),
            tuple(self._spec_map.values())
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
        if executor != ExecutorType.THREAD:
            raise ValueError(
                f"unsupported executor: {executor!r}; "
                f"only ExecutorType.THREAD is supported"
            )

        # 去重键 (uuid, version)：删/未删决定的是“能不能查到”，
        # 不影响生成的缩略图内容，所以不需要把 include_deleted 塞进 key。
        key = (asset_uuid, self._version)
        with self._inflight_lock:
            if key in self._inflight:
                _logger.trace(
                    "thumbnail submit dedup: uuid=%s version=%s",
                    asset_uuid, self._version,
                )
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

        eligible = [
            str(r.uuid) for r in rows
            if not self._is_in_cooldown(str(r.uuid))
        ]
        skipped = len(rows) - len(eligible)
        if skipped:
            _logger.info(
                "thumbnail backfill: %d assets in cooldown, skipped", skipped,
            )
        if not eligible:
            return 0

        _logger.info(
            "thumbnail backfill: submitting %d assets", len(eligible),
        )
        self.submit_bulk_by_uuid(eligible, priority=PRIORITY_BACKFILL)
        return len(eligible)

    def run_once(self, batch: Optional[int] = None) -> dict:
        """同步跑一轮：扫描 → 提交 → 等结果。主要用于测试 / 手动触发。"""
        limit = batch or self._batch_size
        rows = self._repo.list_missing(specs=ALL_SPECS, limit=limit)
        if not rows:
            return {"scanned": 0, "ok": 0, "failed": 0, "skipped": 0}

        futures = self.submit_bulk_by_uuid(
            [str(r.uuid) for r in rows], priority=PRIORITY_BACKFILL,
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
        两种模式下 base 都是共享根，spec 是 ``<xdg-spec>/<md5>.png``，
        仍满足 ``base/rel`` 拼接。
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
    # 永久删除
    # ------------------------------------------------------------------ #
    def purge(self, asset_uuid: str) -> bool:
        """永久删除单个资产的所有缩略图记录（uuid）。

        **仅限永久删除（清空回收站）**。软删（is_deleted=1）不要调这里 ——
        软删必须保留缩略图供 Trash 预览。

        **只清 DB，不删文件（两种模式一致）**。新布局下文件名是源文件的
        md5(uri)，同一源文件被多条 asset 行引用时会共享同一个 PNG；按
        asset 直接 unlink 会波及其它引用。真正该删的文件（源文件已消失
        的孤儿）由与缩略图服务配套的 ThumbnailCleaner 按 PNG 内嵌的
        ``Thumb::URI`` 判定后清理，那是"文件为真相源"的唯一删除入口。

        返回 True 表示资产存在并已处理；False 表示资产不存在。
        """
        paths = self._repo.get_paths_by_uuid(asset_uuid)
        if paths is None:
            return False

        self._repo.clear_and_snapshot_by_uuid(asset_uuid)
        _logger.info(
            "thumbnail db cleared (files kept for cleaner) uuid=%s",
            asset_uuid,
        )
        return True

    def purge_bulk(self, asset_uuids: Sequence[str]) -> int:
        """批量 purge（uuid）；返回处理的资产数。用于 Trash 批量清空。

        同样只清 DB：理由见 :meth:`purge`（共享 md5 文件名 + cleaner 兜底）。
        """
        if not asset_uuids:
            return 0
        rows = self._repo.get_paths_bulk_by_uuid(asset_uuids)
        if not rows:
            return 0

        self._repo.clear_and_snapshot_bulk_by_uuid(list(rows.keys()))
        _logger.info(
            "thumbnail db cleared: %d assets (files kept for cleaner)",
            len(rows),
        )
        return len(rows)

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _work_one(
            self, asset_uuid: str,
            include_deleted: bool = False,
    ) -> ThumbnailResult:
        """TaskService 调用的入口（uuid）。

        去重标记由 submit_by_uuid() 挂在 future 上的 done callback 释放。
        """
        # asset_uuid = asset_uuid if isinstance(asset_uuid, UUID) else UUID(asset_uuid)

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

        # 2.5 短路（两种模式同一套）：fail 标记 + 缓存有效性
        # 其它进程写过的永久失败标记（跨进程 cooldown）
        if xdg_fail_path(src, root=self._root).is_file():
            _logger.info(
                "thumbnail skipped: fail marker asset=%s", asset_uuid,
            )
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error="xdg_fail_marker", retryable=False,
            )

        # 所有 spec 已存在且 mtime 校验通过 → 直接回填路径，省渲染
        if self._all_valid(src):
            base_abs, spec_rels = build_thumb_paths(
                src, self._root, self._spec_map,
            )
            self._repo.update_paths_by_uuid(
                asset_uuid, paths=spec_rels, base_dir=base_abs,
            )
            # 短路路径同样必须广播（见 _publish_ready 的 docstring）：
            # 缩略图已经在盘上，但 DB 里刚回填 → UI 侧可能正等着广播重读。
            self._publish_ready(asset_uuid)
            _logger.trace(
                "thumbnail ready (cached) uuid=%s in %dms",
                asset_uuid, int((time.monotonic() - started) * 1000),
            )
            return ThumbnailResult(
                ok=True, asset_uuid=UUID(asset_uuid), duration_ms=0,
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
                self._maybe_write_fail_marker(src)
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

        # 4. 写盘（带有限重试；图像模式不可编码 → 永久失败并写 fail 标记）
        # 两种模式共用：先算 (base=root, spec_rels)，再交给同一 storage。
        base_abs, spec_rels = build_thumb_paths(
            src, self._root, self._spec_map,
        )

        def _writer() -> str:
            return self._storage.write(src, spec_rels, imgs)

        try:
            base_abs = self._write_with_retry(asset_uuid, _writer)
        except ThumbnailFormatError as exc:
            _logger.error(
                "thumbnail write rejected asset=%s (format): %s",
                asset_uuid, exc,
            )
            self._maybe_write_fail_marker(src)
            return ThumbnailResult(
                ok=False, asset_uuid=UUID(asset_uuid),
                error=f"write_format:{type(exc).__name__}",
                retryable=False,
            )
        except Exception as exc:
            # 环境性失败（磁盘满、权限等），不写 fail 标记：
            # 环境恢复后应允许重试，不能跨进程永久阻断。
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
        self._publish_ready(asset_uuid)

        duration_ms = int((time.monotonic() - started) * 1000)
        _logger.trace(
            "thumbnail done uuid=%s in %dms", task_input.uuid, duration_ms,
        )
        return ThumbnailResult(
            ok=True, asset_uuid=UUID(asset_uuid), duration_ms=duration_ms,
        )

    def _publish_ready(self, asset_uuid: str) -> None:
        """广播 ``thumbnail_ready``（uuid）：路径已落库，订阅方可以重读。

        契约：``cb(sender: ThumbnailService, *, asset_uuid: str)``。

        **两条成功路径都必须调用**：渲染+写盘，以及「缓存已有效、只回填
        路径」的短路。后者早期漏发，导致一个很隐蔽的死等：

        1. 启动 backfill 先把该 uuid 的生成任务提交进 ``_inflight``；
        2. UI 加载时磁盘/DB 还没路径 → ``submit_by_uuid`` 命中 in-flight
           去重，返回 ``None``，UI 只能「等广播」（见 image_presenter）；
        3. 任务走短路分支，回填路径后直接 return，**不广播**；
        4. UI 永远等不到事件 → 缩略图在盘上、DB 里也有路径，界面上却是空白，
           只有滚动/重进视图才会重新加载。

        因此广播的触发条件不是「我渲染了」，而是「路径现在可读了」。
        """
        try:
            self.thumbnail_ready.emit(self, asset_uuid=asset_uuid)
        except Exception:
            _logger.exception(
                "emit thumbnail_ready failed asset=%s", asset_uuid,
            )

    # ------------------------------------------------------------------ #
    # 缓存有效性 / 永久失败标记
    # ------------------------------------------------------------------ #
    def _all_valid(self, src: Path) -> bool:
        """所有 spec 的缩略图存在且 mtime 校验通过。

        两种模式共用：路径由 build_thumb_paths 按 self._root 决定，
        有效性判定看 PNG 内嵌的 ``Thumb::MTime`` 是否等于源文件 mtime。
        """
        base_abs, spec_rels = build_thumb_paths(
            src, self._root, self._spec_map,
        )
        root = Path(base_abs)
        for rel in spec_rels.values():
            p = root / rel
            if not p.is_file():
                return False
            if not is_thumbnail_valid_for(p, src):
                return False
        return True

    def _maybe_write_fail_marker(self, src: Path) -> None:
        """永久失败时写 ``<root>/fail/<md5>.png`` 跨进程标记。"""
        self._storage.write_fail_marker(src)

    # ------------------------------------------------------------------ #
    # 写盘：有限重试（两种模式共用）
    # ------------------------------------------------------------------ #
    def _write_with_retry(
            self,
            asset_uuid: str,
            writer_fn: Callable[[], str],
    ) -> str:
        """首次 + write_max_retries 次重试，指数退避。

        ``writer_fn`` 负责实际写盘并返回 base_abs；``ThumbnailFormatError``
        视为永久失败直接上抛，其余 ``OSError`` 视为暂时性失败退避重试。
        """
        attempts = self._write_max_retries + 1
        last_exc: Optional[BaseException] = None

        for i in range(attempts):
            try:
                return writer_fn()
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
