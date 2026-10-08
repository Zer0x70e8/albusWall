# 
"""XDG 缩略图缓存的孤儿清理。

真相源是**文件系统**：读每个 PNG 内嵌的 ``Thumb::URI``，
检查源文件是否还存在。与 SourceTrashService 的 DB 真相源无关。

平台判断由 bootstrap 完成；本模块通过 ``config.root is None``
得知是否被禁用。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from albuswall.log import getLogger, Logger
from albuswall.utils.xdg_thumbnail import (
    XDG_SPECS,
    read_thumbnail_info,
    uri_to_path,
)

from .config import XdgCleanerConfig

_logger = getLogger(__name__)

_HEX_LEN = 32
_HEX_CHARS = set("0123456789abcdef")


@dataclass
class OrphanEntry:
    path: Path
    size: int
    source_uri: str
    source_path: Optional[str]


@dataclass
class OrphanCleanupReport:
    scanned: int = 0
    orphans: list[OrphanEntry] = field(default_factory=list)
    deleted: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def orphan_bytes(self) -> int:
        return sum(o.size for o in self.orphans)


class XdgThumbnailCleaner:
    """XDG 缩略图孤儿清理器。配置通过构造参数注入。"""

    def __init__(
            self,
            config: XdgCleanerConfig,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._config = config
        self._log = log or _logger

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def scan(self) -> OrphanCleanupReport:
        """扫描并返回孤儿清单（不删）。禁用时返回空报告。"""
        report = OrphanCleanupReport()
        if not self._config.enabled:
            self._log.debug("XDG cleaner disabled, skip scan")
            return report

        root = self._config.root
        assert root is not None  # enabled 已保证
        if not root.is_dir():
            self._log.debug("XDG thumbnail root %s missing, skip", root)
            return report

        now = time.time()
        for spec in XDG_SPECS:
            self._scan_dir(root / spec, report, now)
        self._scan_dir(root / "fail", report, now)
        return report

    def purge(self, orphans: Iterable[OrphanEntry]) -> tuple[int, int]:
        """删除孤儿文件。返回 ``(deleted, failed)``。"""
        deleted = 0
        failed = 0
        for entry in orphans:
            try:
                os.unlink(entry.path)
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                failed += 1
                self._log.warning("failed to unlink %s: %s", entry.path, exc)
        self._try_rmdir_specs()
        return deleted, failed

    def scan_and_purge(self) -> OrphanCleanupReport:
        report = self.scan()
        if report.orphans:
            report.deleted, report.failed = self.purge(report.orphans)
        self._log.info(
            "XDG orphan cleanup: scanned=%d orphans=%d deleted=%d "
            "failed=%d skipped=%d bytes=%d",
            report.scanned, len(report.orphans), report.deleted,
            report.failed, report.skipped, report.orphan_bytes,
        )
        return report

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _scan_dir(
            self,
            directory: Path,
            report: OrphanCleanupReport,
            now: float,
    ) -> None:
        if not directory.is_dir():
            return
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            self._log.warning("cannot list %s: %s", directory, exc)
            return

        min_age = self._config.min_age_seconds
        for entry in entries:
            report.scanned += 1
            if not self._is_candidate(entry):
                report.skipped += 1
                continue
            try:
                st = entry.stat()
            except OSError:
                report.skipped += 1
                continue
            if now - st.st_mtime < min_age:
                report.skipped += 1
                continue

            info = read_thumbnail_info(entry)
            if info is None or not info.uri:
                report.skipped += 1
                continue
            local_path = uri_to_path(info.uri)
            if local_path is None:
                report.skipped += 1
                continue
            if Path(local_path).exists():
                continue

            report.orphans.append(OrphanEntry(
                path=entry,
                size=st.st_size,
                source_uri=info.uri,
                source_path=local_path,
            ))

    def _try_rmdir_specs(self) -> None:
        root = self._config.root
        if root is None:
            return
        for spec in XDG_SPECS:
            try:
                os.rmdir(root / spec)
            except OSError:
                pass
        try:
            os.rmdir(root / "fail")
        except OSError:
            pass

    @staticmethod
    def _is_candidate(entry: Path) -> bool:
        if not entry.is_file():
            return False
        name = entry.name
        if not name.endswith(".png") or len(name) != _HEX_LEN + 4:
            return False
        return all(c in _HEX_CHARS for c in name[:_HEX_LEN])
