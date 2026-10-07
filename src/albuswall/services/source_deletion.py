"""Source 回收站：软删源 → 异步硬删 DB 行 + 缩略图，保留源文件。

职责边界
--------
* 承接「删除源」意图：立刻软删（is_deleted=1，UI 上消失），
  再交给 Reconciler 调度一次后台 purge。
* 承接「恢复源」意图：purge 未执行前允许撤销。
* 承接启动补交：上次进程被 kill 时，源可能停在 is_deleted=1
  但物理行仍在——启动时扫一遍 list_pending_purge_ids()。

**不删源文件**。source_path 目录下的原始素材属于用户数据。
**不软删 assets**。源级回收站直接把旗下 assets 一起硬删。
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from albuswall.repositories import IngestSourceRepository, ThumbnailRepository
from albuswall.dto.source import MANUAL_SOURCE_ID, SourcePurgeReport
from albuswall.dto.thumbnail import ThumbnailPaths
from albuswall.infrastructure.reconcile.service import Reconciler
from albuswall.infrastructure.task.protocol import TaskServiceProtocol
from albuswall.log import getLogger, Logger

_logger: Logger = getLogger(__name__)

RECONCILER_NAME = "source_trash"


class SourceTrashService:
    """源级回收站。对外只暴露四件事。"""

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            thumbnail_repo: ThumbnailRepository,
            task_service: TaskServiceProtocol,
            reconciler: Reconciler,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._sources = source_repo
        self._thumbs = thumbnail_repo
        self._tasks = task_service
        self._reconciler = reconciler
        self._log = log or _logger
        self._lock = threading.Lock()
        self._shutdown = False
        self._register_reconciler()

    # ================================================================
    # 公共 API
    # ================================================================
    def delete_source(self, source_id: int) -> bool:
        """用户点「删除」：软删 + 调度异步 purge。

        Returns:
            True  源从活跃集消失，purge 已排队（或排队失败但重启后补交）。
            False 源不存在 / 已是删除态 / 手动源被拒。

        Raises:
            ValueError: id == MANUAL_SOURCE_ID（虚拟根禁止删除）。
        """
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot delete manual/virtual source (id={MANUAL_SOURCE_ID})"
            )

        # 仓储层软删：只在 is_deleted=0 时生效，天然幂等。
        if not self._sources.soft_delete(source_id):
            return False

        self._log.info("source %d soft-deleted, scheduling purge", source_id)
        self._schedule(source_id)
        return True

    def restore_source(self, source_id: int) -> bool:
        """撤销软删。purge 已执行时返回 False（行已不存在）。"""
        restored = self._sources.restore(source_id)
        if restored:
            self._log.info("source %d restored from trash", source_id)
        return restored

    def purge_source_now(self, source_id: int) -> SourcePurgeReport:
        """同步硬删，绕过调度器。

        仅给管理端点 / 测试用——生产路径应走 delete_source。
        注意：本方法在主线程跑文件 IO，可能阻塞几秒。
        """
        return self._purge_one(source_id)

    def shutdown(self) -> None:
        """只置标志；进行中的 purge 由 task_service 负责收尾。"""
        with self._lock:
            self._shutdown = True

    # ================================================================
    # Reconciler 注册
    # ================================================================
    def _register_reconciler(self) -> None:
        self._reconciler.register(
            RECONCILER_NAME,
            incremental=self._purge_batch,
            full=self._purge_all_pending,
            on_startup=True,       # 启动时全量补交
            debounce=1.0,          # 用户连点删除时合并
            priority=8,            # 低于用户可见任务（默认 0）
        )

    def _schedule(self, source_id: int) -> None:
        """入队。失败时不抛——重启时 on_startup 会再扫一遍。"""
        try:
            self._reconciler.invalidate(RECONCILER_NAME, str(source_id))
        except Exception:
            self._log.exception(
                "failed to enqueue purge for source %d; "
                "will be retried on next startup", source_id,
            )

    # ================================================================
    # Reconciler 回调
    # ================================================================
    def _purge_batch(self, source_ids: set[str]) -> None:
        """增量：一批已知待删的 source_id。"""
        for raw in source_ids:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                self._log.warning("ignore malformed source id in trash queue: %r", raw)
                continue
            try:
                self._purge_one(sid)
            except Exception:
                # 单条失败不拖垮整批；full 兜底会再扫
                self._log.exception("purge source %d failed", sid)

    def _purge_all_pending(self) -> None:
        """全量：扫表，找所有 is_deleted=1 但物理行仍在的源。"""
        ids = self._sources.list_pending_purge_ids()
        if not ids:
            return
        self._log.info("found %d pending source purge(s) at startup", len(ids))
        for sid in ids:
            try:
                self._purge_one(sid)
            except Exception:
                self._log.exception("purge source %d failed", sid)

    # ================================================================
    # 核心：硬删一个源
    # ================================================================
    def _purge_one(self, source_id: int) -> SourcePurgeReport:
        """顺序很重要：先快照缩略图，再删 DB，最后删磁盘。

        为什么 DB 先删：
          · DB 删干净后文件删失败，只是垃圾文件，可以事后对账
          · 文件先删而 DB 删失败 → 行还在但缩略图丢了，不可逆
        """
        # 1. 快照（purge 之后 assets 行没了，查不到 thumb_path）
        thumbs = self._thumbs.get_paths_by_source(source_id)

        # 2. 硬删 DB（assets + ingest_source；candidate_cache 由 CASCADE 顺带清）
        report = self._sources.purge(source_id)

        if not report["purged"]:
            # 源不存在 / 已被 restore / 从未软删 —— 幂等空转
            if thumbs:
                # 只在"源不在了但还有快照"这种诡异态下打一条 debug
                self._log.debug(
                    "source %d purge no-op, but %d thumbnail dirs were snapshot",
                    source_id, len(thumbs),
                )
            return report

        self._log.info(
            "purged source %d: %d asset rows, %d thumb dirs",
            source_id, len(report["asset_ids"]), len(thumbs),
        )

        # 3. 删磁盘缩略图（best-effort；失败只记录，不影响事务结果）
        self._remove_thumbnail_files(source_id, thumbs)
        return report

    # ================================================================
    # 文件系统：只删缩略图，绝不碰 source_path
    # ================================================================
    def _remove_thumbnail_files(
            self,
            source_id: int,
            paths: dict[str, ThumbnailPaths],
    ) -> None:
        """best-effort 删文件。任何失败都吞掉（只 warning）。

        注意：ThumbnailPaths.base 里已经带了 uuid，是资产独有的目录，
        所以两个资产不会共享同一个 base，逐资产删即可。
        """
        if not paths:
            return

        removed_files = 0
        removed_dirs = 0
        for asset_uuid, tp in paths.items():
            base = getattr(tp, "base", None)
            if not base:
                continue
            # 删各 spec 文件
            for spec, rel in tp.items():     # {spec: rel_path}
                if not rel:
                    continue
                abs_path = os.path.join(base.rstrip("/\\"), rel)
                try:
                    os.unlink(abs_path)
                    removed_files += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self._log.warning(
                        "failed to unlink thumbnail %s (source=%d, uuid=%s): %s",
                        abs_path, source_id, asset_uuid, exc,
                    )
            # 删 base 目录（只在空时成功；有残留文件时静默跳过）
            try:
                os.rmdir(base)
                removed_dirs += 1
            except OSError:
                pass

        self._log.debug(
            "source %d: removed %d thumbnail files, %d dirs",
            source_id, removed_files, removed_dirs,
        )
