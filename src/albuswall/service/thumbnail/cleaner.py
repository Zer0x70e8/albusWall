#
"""缩略图缓存的**孤儿**清理 + 会话脏标判定。

真相源是**文件系统**，不是 DB
-----------------------------
本模块读每个 PNG 里内嵌的 ``Thumb::URI``，检查源文件是否还存在；不存在就是
孤儿。与 ``ThumbnailService.purge`` 的分工：

* ``purge`` 只清 **DB 记录**（共享 md5 文件名可能被多条 asset 行引用，按 asset
  unlink 会波及其它引用）；
* 本模块只清 **文件**，判定依据是"这个 PNG 指向的源文件还在不在"。

两者互不调用，因此"源文件没了但 DB 行还在"和"DB 行删了但 PNG 还在"都能各自
收敛，不会变成死循环。

什么时候跑
----------
**不是**定时任务，也不随删除触发。只有两个入口：

1. 会话脏标（``SessionMarker``）：上次退出不干净 → 开机跑一次
   :meth:`ThumbnailCleaner.scan_and_purge`（进程被杀时正在写的 PNG 可能
   半截/无主），由装配层的 ``on_boot`` 驱动；
2. 显式调用（管理端点 / 测试）。

为什么不在删除时跑：删除时源文件通常还在，判定不出孤儿；而且清理要遍历整个
缓存目录（可能上万文件），放在删除路径上会明显拖慢用户操作。

根目录与模式
------------
``root`` 由调用方注入（私有模式 = ``thumb_root``，XDG 模式 =
``xdg_thumbnails_root()``），与 ``ThumbnailService`` 严格同源。本模块
**不** import 缩略图配置，只认 ``ThumbnailCleanerConfig.root``。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError
from albuswall.log import Logger, getLogger
from albuswall.utils.platform_paths import platform_cache_root
from albuswall.utils.session_marker import SessionMarker
from albuswall.utils.xdg_thumbnail import (
    XDG_SPECS,
    read_thumbnail_info,
    uri_to_path,
    xdg_thumbnails_root,
)

from .config import resolve_thumb_root, thumbnail_config

__all__ = [
    "CleanerSettings",
    "OrphanCleanupReport",
    "OrphanEntry",
    "SessionConfig",
    "ThumbnailCleaner",
    "ThumbnailCleanerConfig",
    "load_cleaner_config",
    "load_session_config",
]

_logger: Logger = getLogger(__name__)

_HEX_LEN = 32
_HEX_CHARS = set("0123456789abcdef")

#: 本模块声明并消费的 ``[source_trash]`` 键。
_FIELDS: Tuple[str, ...] = (
    "xdg_compat", "orphan_min_age_seconds",
    "session_marker_path", "auto_cleanup_on_dirty",
)


@configclass("source_trash", "main")
class CleanerSettings:
    """``[source_trash]`` 段里**缩略图维护**相关的键（本模块自己声明）。

    为什么敢和 ``SourceTrashSettings`` 共用同一个 section：``configue`` 允许
    同一 ``(section, key)`` 被多个 ``@configclass`` 声明（共享同一个 Resolver
    节点），只要求同名键的 type / default **等价**，否则装饰期抛
    ``ConfigDeclarationConflict``。两边声明的是**不同**的键，互不覆盖，于是
    本域不必 import 源回收站的配置模块。

    注意 ``prune_empty_dirs``（旧实现声明过）**没有移植**：清理器删除后无条件
    修剪空 spec 目录（见 :meth:`ThumbnailCleaner._try_rmdir_specs`），那个开关
    从来没被读过，留着就是死配置。
    """

    #: ``None`` = 按平台默认（当前全平台一致，等价 True）；``False`` = 用户
    #: 显式关闭整条孤儿清理链路。
    xdg_compat: Optional[bool] = None
    #: 孤儿缩略图的最小年龄（秒）：刚写下的 PNG 不动，避免清掉正在生成的。
    orphan_min_age_seconds: float = 600.0
    #: 会话标记文件；``None`` = ``<platform cache>/albuswall/.session``。
    session_marker_path: Optional[str] = None
    #: 上次非正常退出时，是否自动跑一次孤儿清理。
    auto_cleanup_on_dirty: bool = True


def _declared_defaults() -> Dict[str, Any]:
    """声明期默认值：``Configue`` 尚未构造时的兜底（单测 / 无配置环境）。"""
    result: Dict[str, Any] = {}
    for name in _FIELDS:
        slot = getattr(CleanerSettings, name, None)
        result[name] = getattr(slot, "default", slot)
    return result


_CLEANER_DEFAULTS: Dict[str, Any] = _declared_defaults()


def _cleaner_settings() -> Dict[str, Any]:
    """``[source_trash]`` 段缩略图维护键的快照（带兜底 + WARNING）。

    兜底范围与其它配置读取点一致（``ConfigError`` + 裸 ``ValueError``）：
    编程错误不兜，当场炸。
    """
    try:
        cfg = CleanerSettings()
        return {name: getattr(cfg, name) for name in _FIELDS}
    except (ConfigError, ValueError) as exc:
        _logger.warning(
            "cleaner settings: config system unavailable or invalid "
            "(%s: %s); falling back to declared defaults %s",
            type(exc).__name__, exc, _CLEANER_DEFAULTS,
        )
        return dict(_CLEANER_DEFAULTS)


@dataclass(frozen=True)
class ThumbnailCleanerConfig:
    """清理器的运行时配置。``enabled=False`` 或 ``root is None`` 时全部 no-op。"""

    enabled: bool = True
    root: Optional[Path] = None
    min_age_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.min_age_seconds < 0:
            raise ValueError("min_age_seconds must be >= 0")
        if self.enabled and self.root is None:
            raise ValueError("root is required when enabled is True")


@dataclass(frozen=True)
class SessionConfig:
    """会话脏标 + 崩溃恢复策略。"""

    marker_path: Path
    auto_cleanup_on_dirty: bool = True

    def __post_init__(self) -> None:
        if not self.marker_path.name:
            raise ValueError("marker_path must be a file path")


def load_cleaner_config(
        settings: Optional[Dict[str, Any]] = None,
) -> ThumbnailCleanerConfig:
    """算好清理器的 root 与阈值。

    根目录在此处**一次性**算好并注入：cleaner 只认 ``root``，不 import 缩略图
    配置。两种模式的布局同构（``<root>/<spec>/<md5>.png``），所以模式差异就
    只剩这一行 root 的选择 —— 与 ``ThumbnailService`` 严格同源。
    """
    if settings is None:
        settings = _cleaner_settings()

    if settings["xdg_compat"] is False:      # 用户显式关闭 → 整条链路 no-op
        return ThumbnailCleanerConfig(enabled=False, root=None)

    thumb = thumbnail_config()
    root = (
        xdg_thumbnails_root() if thumb["xdg_enabled"]
        else resolve_thumb_root(thumb)
    )
    return ThumbnailCleanerConfig(
        enabled=True,
        root=root,
        min_age_seconds=float(settings["orphan_min_age_seconds"]),
    )


def load_session_config(
        settings: Optional[Dict[str, Any]] = None,
) -> SessionConfig:
    """会话标记配置。"""
    if settings is None:
        settings = _cleaner_settings()
    marker_path = settings["session_marker_path"]
    path = (
        Path(marker_path) if marker_path
        else platform_cache_root() / "albuswall" / ".session"
    )
    return SessionConfig(
        marker_path=path,
        auto_cleanup_on_dirty=bool(settings["auto_cleanup_on_dirty"]),
    )


# ======================================================================
# 孤儿清单与清理器
# ======================================================================

@dataclass
class OrphanEntry:
    """一个孤儿 PNG：源文件已不存在。"""

    path: Path
    size: int
    source_uri: str
    source_path: Optional[str]


@dataclass
class OrphanCleanupReport:
    """一次扫描 / 清理的结果。"""

    scanned: int = 0
    orphans: list[OrphanEntry] = field(default_factory=list)
    deleted: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def orphan_bytes(self) -> int:
        return sum(o.size for o in self.orphans)


class ThumbnailCleaner:
    """缩略图孤儿清理器。配置（含 root）通过构造参数注入。

    两种模式的扫描 / 清理逻辑完全相同，因为布局已经同构：
    ``<root>/{normal,large,x-large,xx-large,fail}/<md5>.png``。
    """

    def __init__(
            self,
            config: ThumbnailCleanerConfig,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._config = config
        self._log = log or _logger

    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"ThumbnailCleaner(enabled={self._config.enabled}, "
            f"root={self._config.root})"
        )
        if verbose:
            base = base[:-1] + f", min_age={self._config.min_age_seconds})"
        return base

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def scan(self) -> OrphanCleanupReport:
        """扫描并返回孤儿清单（不删）。禁用时返回空报告。"""
        report = OrphanCleanupReport()
        if not self._config.enabled:
            self._log.debug("thumbnail cleaner disabled, skip scan")
            return report

        root = self._config.root
        assert root is not None  # enabled 已保证
        if not root.is_dir():
            self._log.debug("thumbnail root %s missing, skip", root)
            return report

        now = time.time()
        for spec in XDG_SPECS:
            self._scan_dir(root / spec, report, now)
        self._scan_dir(root / "fail", report, now)
        return report

    def purge(self, orphans: list[OrphanEntry] | Any) -> tuple[int, int]:
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
        """扫描 + 清理一趟（开机脏恢复用的就是它）。"""
        report = self.scan()
        if report.orphans:
            report.deleted, report.failed = self.purge(report.orphans)
        self._log.info(
            "thumbnail orphan cleanup: scanned=%d orphans=%d deleted=%d "
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
                # 太新：可能是正在写入的缩略图，跳过。
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
        """修剪空的 spec 目录（best-effort）。"""
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
        """``<32hex>.png`` 才可能是缩略图（``fail/`` 目录同构）。"""
        if not entry.is_file():
            return False
        name = entry.name
        if not name.endswith(".png") or len(name) != _HEX_LEN + 4:
            return False
        return all(c in _HEX_CHARS for c in name[:_HEX_LEN])


def install_dirty_recovery(container: Any) -> None:
    """把「脏退出后清孤儿」挂成 ``on_boot`` / ``on_exit``。

    顺序关键（``on_boot``）：**先读**会话标记，再置脏；反过来的话永远读到
    自己刚写的 dirty。

    ``on_exit`` 只写 ``clean``：即使清理失败也要写，否则下次启动会白跑一遍
    全量扫描（那是几分钟的 IO）。
    """
    from albuswall.core import Application

    session_cfg = load_session_config()
    marker = SessionMarker(session_cfg.marker_path)

    def _on_boot() -> None:
        # 三态而不是两态：**标记文件不存在 = 没有"上次运行"可言**，不该当成
        # "脏退出"。首次安装 / 换了缓存目录时去扫一遍空缓存没有意义，而在
        # 测试环境里扫真实缓存则是纯粹的副作用。
        has_marker = session_cfg.marker_path.exists()
        if not has_marker:
            _logger.debug(
                "no session marker (%s); treat as first run, skip cleanup",
                session_cfg.marker_path,
            )
        elif marker.is_clean_shutdown():
            _logger.debug("clean shutdown, skip thumbnail orphan cleanup")
        elif session_cfg.auto_cleanup_on_dirty:
            _logger.info("dirty shutdown, running thumbnail orphan cleanup")
            try:
                container.get("thumbnail_cleaner").scan_and_purge()
            except Exception:  # noqa: BLE001 —— 清理失败不该阻止启动
                _logger.exception("thumbnail orphan cleanup failed")
        marker.mark_running()

    def _on_exit() -> None:
        marker.mark_clean()

    # 钩子是 ``Application`` 的 classmethod；门面类本身才是对的宿主。
    Application.on_boot(_on_boot)
    Application.on_exit(_on_exit)
