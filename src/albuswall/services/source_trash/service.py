#
"""Source 回收站：软删源 → 异步硬删 DB 行 + 缩略图，保留源文件。

所有配置由 ``SourceTrashConfig`` 注入；本模块不读环境变量、
不做平台判断、不计算路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from albuswall.repositories.source import IngestSourceRepository
from albuswall.repositories.thumbnail import ThumbnailRepository
from albuswall.dto.source import MANUAL_SOURCE_ID, SourcePurgeReport
from albuswall.dto.thumbnail import ThumbnailPaths
from albuswall.infrastructure.reconcile.service import Reconciler
from albuswall.log import getLogger, Logger
from albuswall.utils.signals import SignalDescriptor

from .config import SourceTrashConfig

_logger: Logger = getLogger(__name__)


class SourceTrashService:
    """源级回收站。配置与协作者全部由构造参数注入。"""
    sources_changed = SignalDescriptor("sources_changed", cross_process=True)

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            thumbnail_repo: ThumbnailRepository,
            reconciler: Reconciler,
            config: SourceTrashConfig,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._sources = source_repo
        self._thumbs = thumbnail_repo
        self._reconciler = reconciler
        self._config = config
        self._log = log or _logger
        self._register_reconciler()

    # ================================================================
    # 公共 API
    # ================================================================
    def delete_source(self, source_id: int) -> bool:
        """软删 + 调度异步 purge。手动源抛 ``ValueError``。"""
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot delete manual/virtual source (id={MANUAL_SOURCE_ID})"
            )
        if not self._sources.soft_delete(source_id):
            return False

        self._log.info("source %d soft-deleted, scheduling purge", source_id)
        self._schedule(source_id)
        self.sources_changed.send(self, source_id=source_id, kind="soft_delete")
        return True

    def restore_source(self, source_id: int) -> bool:
        """撤销软删。行已硬删时返回 False。"""
        restored = self._sources.restore(source_id)
        if restored:
            self._log.info("source %d restored from trash", source_id)
            self.sources_changed.send(self, source_id=source_id, kind="restore")
        return restored

    def purge_source_now(self, source_id: int) -> SourcePurgeReport:
        """同步硬删。仅给管理端点 / 测试用。"""
        return self._purge_one(source_id)

    # ================================================================
    # Reconciler 集成
    # ================================================================
    def _register_reconciler(self) -> None:
        self._reconciler.register(
            self._config.reconciler_name,
            incremental=self._purge_batch,
            full=self._purge_all_pending,
            on_startup=True,
            debounce=self._config.debounce,
            priority=self._config.priority,
        )

    def _schedule(self, source_id: int) -> None:
        try:
            self._reconciler.invalidate(
                self._config.reconciler_name, str(source_id),
            )
        except Exception:
            self._log.exception(
                "failed to enqueue purge for source %d; "
                "on_startup will retry", source_id,
            )

    def _purge_batch(self, source_ids: set[str]) -> None:
        for raw in source_ids:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                self._log.warning("ignore malformed source id: %r", raw)
                continue
            try:
                self._purge_one(sid)
            except Exception:
                self._log.exception("purge source %d failed", sid)

    def _purge_all_pending(self) -> None:
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
    # 核心
    # ================================================================
    def _purge_one(self, source_id: int) -> SourcePurgeReport:
        """先快照缩略图 → 硬删 DB → 后删磁盘。

        DB 先删是刻意的：DB 删干净后文件删失败只是垃圾文件，可事后
        对账；文件先删而 DB 删失败则是不可逆的数据丢失。
        """
        thumbs = self._thumbs.get_paths_by_source(source_id)
        report = self._sources.purge(source_id)

        if not report["purged"]:
            return report

        self._log.info(
            "purged source %d: %d asset rows, %d thumb dirs",
            source_id, len(report["asset_ids"]), len(thumbs),
        )
        self._remove_thumbnail_files(source_id, thumbs)
        self.sources_changed.send(self, source_id=source_id, kind="purged")
        return report

    def _remove_thumbnail_files(
            self,
            source_id: int,
            paths: dict[str, ThumbnailPaths],
    ) -> None:
        """best-effort 删私有缩略图。任何失败都吞掉。

        XDG 防护：若 base 落在 XDG 缓存内，说明 thumb_root 配错了，
        拒绝删除并记 warning——XDG 缓存是跨应用共享的。
        平台判断由 bootstrap 完成；这里只判断路径形态。
        """
        if not paths:
            return

        removed_files = 0
        removed_dirs = 0
        skipped_xdg = 0

        for asset_uuid, tp in paths.items():
            base = getattr(tp, "base", None)
            if not base:
                continue

            # XDG 防护用惰性导入，避免 utils 反向依赖服务
            from albuswall.utils.xdg_thumbnail import is_inside_xdg_cache
            if is_inside_xdg_cache(base):
                skipped_xdg += 1
                self._log.warning(
                    "refuse to delete thumbnail under XDG cache "
                    "(source=%d, uuid=%s, base=%s); thumb_root "
                    "is likely misconfigured",
                    source_id, asset_uuid, base,
                )
                continue

            for spec, rel in tp.items():
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
                        "failed to unlink thumbnail %s "
                        "(source=%d, uuid=%s): %s",
                        abs_path, source_id, asset_uuid, exc,
                    )

            try:
                os.rmdir(base)
                removed_dirs += 1
            except OSError:
                pass

        self._log.debug(
            "source %d: removed %d files, %d dirs, skipped_xdg=%d",
            source_id, removed_files, removed_dirs, skipped_xdg,
        )
