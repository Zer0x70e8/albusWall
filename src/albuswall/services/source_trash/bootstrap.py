#
"""source_trash 的配置加载层。

本模块是包内**唯一**允许读 config 系统、环境变量、平台信息的
地方。对外只暴露**参数工具**（``load_*``）与一个**生命周期工具**
（``install_source_trash_lifecycle``）；服务本身的构造由调用方
（``service/__init__.py`` 的 ``_ARG_MAP``）决定。
"""

from __future__ import annotations

from pathlib import Path

from albuswall.configue import ConfigField
from albuswall.core import Container, Application
from albuswall.log import getLogger

from albuswall.utils.platform_paths import platform_cache_root
from albuswall.utils.session_marker import SessionMarker

from .config import SourceTrashConfig, XdgCleanerConfig, SessionConfig
from ...utils.thumbnail_paths import uses_xdg_thumbnails, thumbnail_root

_logger = getLogger(__package__)


class _Config:
    reconciler_name: str = ConfigField(
        "source_trash", "reconciler_name", default="source_trash",
    )
    priority: int = ConfigField("source_trash", "priority", default=8)
    debounce: float = ConfigField("source_trash", "debounce", default=1.0)

    # None = 按平台默认；True/False = 显式开关
    xdg_compat: bool | None = ConfigField(
        "source_trash", "xdg_compat", default=None,
    )
    orphan_min_age_seconds: float = ConfigField(
        "source_trash", "orphan_min_age_seconds", default=600.0,
    )

    session_marker_path: str | None = ConfigField(
        "source_trash", "session_marker_path", default=None,
    )
    auto_cleanup_on_dirty: bool = ConfigField(
        "source_trash", "auto_cleanup_on_dirty", default=True,
    )

    # 空目录修剪开关（缩略图删除后是否顺带修剪父目录）
    prune_empty_dirs: bool = ConfigField(
        "source_trash", "prune_empty_dirs", default=True,
    )


_cfg = _Config()


# =====================================================================
# 参数工具：环境 → 值对象
# =====================================================================
def load_source_trash_config() -> SourceTrashConfig:
    """SourceTrashService 的运行时配置。"""
    return SourceTrashConfig(
        reconciler_name=_cfg.reconciler_name,
        priority=_cfg.priority,
        debounce=_cfg.debounce,
    )


def load_xdg_cleaner_config() -> XdgCleanerConfig:
    """XdgThumbnailCleaner 的运行时配置。

    平台判断只在这里做一次。只有走 XDG 协议的平台才有 cleaner
    的用武之地；其他平台直接传 root=None 让它 no-op。
    """
    if not uses_xdg_thumbnails():
        return XdgCleanerConfig(root=None)

    if _cfg.xdg_compat is False:      # 用户显式关闭
        return XdgCleanerConfig(root=None)

    return XdgCleanerConfig(
        root=thumbnail_root(),
        min_age_seconds=_cfg.orphan_min_age_seconds,
    )


def load_session_config() -> SessionConfig:
    """会话标记配置。"""
    if _cfg.session_marker_path:
        path = Path(_cfg.session_marker_path)
    else:
        path = platform_cache_root() / "albuswall" / ".session"
    return SessionConfig(
        marker_path=path,
        auto_cleanup_on_dirty=_cfg.auto_cleanup_on_dirty,
    )


# =====================================================================
# 生命周期工具
# =====================================================================
def install_source_trash_lifecycle(
    container: Container,
    *,
    app: Application | None = None,
) -> None:
    """挂载 session marker 的 on_boot / on_final 钩子。

    与 ``_ARG_MAP`` 分工：``_ARG_MAP`` 只负责**构造**；副作用钩子
    走本函数显式安装。顺序上需在 ``register_all`` 之后调用，保证
    容器里已有 ``xdg_thumbnail_cleaner`` 可用。
    """
    if app is None:
        app = Application.instance()

    session_cfg = load_session_config()
    marker = SessionMarker(session_cfg.marker_path)

    def _on_boot() -> None:
        # 顺序关键：先读，再置脏。反了的话永远读到自己写的 dirty。
        clean = marker.is_clean_shutdown()
        if clean:
            _logger.debug("clean shutdown, skip XDG orphan cleanup")
        elif session_cfg.auto_cleanup_on_dirty:
            _logger.info("dirty shutdown, running XDG orphan cleanup")
            # noinspection broad-exception
            try:
                container.get("xdg_thumbnail_cleaner").scan_and_purge()
            except Exception:
                _logger.exception("XDG orphan cleanup failed")
        marker.mark_running()

    def _on_final() -> None:
        marker.mark_clean()

    app.on_boot(_on_boot)
    app.on_final(_on_final)
