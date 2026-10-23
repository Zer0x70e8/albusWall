#
"""source_trash 的配置加载层。

本模块是包内**唯一**允许读 config 系统、环境变量、平台信息的
地方。对外只暴露**参数工具**（``load_*``）与一个**生命周期工具**
（``install_source_trash_lifecycle``）；服务本身的构造由调用方
（``service/__init__.py`` 的 ``_ARG_MAP``）决定。

取值分两步：``source_trash_config()`` 先拿到一份 typed dict 快照
（默认值 / 类型来自 ``_Config`` 的 ``ConfigField`` 声明），再由
``load_*`` 把它和环境信息一起折叠成 frozen dataclass。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from albuswall.configue import ConfigField
from albuswall.configue.typed import load_typed
from albuswall.core import Application, Container
from albuswall.log import getLogger
from albuswall.services.schema import SourceTrashServiceConfig
from albuswall.utils.platform_paths import platform_cache_root
from albuswall.utils.session_marker import SessionMarker
from albuswall.utils.xdg_thumbnail import xdg_thumbnails_root

from ..thumbnail.config import resolve_thumb_root, thumbnail_config
from .config import SessionConfig, SourceTrashConfig, ThumbnailCleanerConfig

_logger = getLogger(__package__)


# noinspection bad-assignment
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


def source_trash_config() -> SourceTrashServiceConfig:
    """source_trash 配置的 typed dict 快照。

    默认值 / 类型全部来自 ``_Config`` 的 ``ConfigField`` 声明；只绑定这
    一个 owner，不会串到别的服务的同名字段。
    """
    return cast(
        SourceTrashServiceConfig,
        load_typed(SourceTrashServiceConfig, _Config),
    )


# =====================================================================
# 参数工具：环境 → 值对象
# =====================================================================
def load_source_trash_config(
        conf: Optional[SourceTrashServiceConfig] = None,
) -> SourceTrashConfig:
    """SourceTrashService 的运行时配置。"""
    if conf is None:
        conf = source_trash_config()
    return SourceTrashConfig(
        reconciler_name=conf["reconciler_name"],
        priority=conf["priority"],
        debounce=conf["debounce"],
    )


def load_thumbnail_cleaner_config(
        conf: Optional[SourceTrashServiceConfig] = None,
) -> ThumbnailCleanerConfig:
    """ThumbnailCleaner 的运行时配置。

    根目录在此处**一次性**算好并注入：cleaner 只认 root，不 import
    thumbnail 配置。两种模式的布局同构（<root>/<spec>/<md5>.png），
    所以模式差异就只剩这一行 root 的选择 —— 与 ThumbnailService 严格同源。

    平台判断（macOS / Windows 也走同一套布局）已不再需要：协议语义
    全平台一致，只有根目录按平台惯例变化（见 xdg_thumbnail）。
    """
    if conf is None:
        conf = source_trash_config()

    enabled = conf["xdg_compat"] is not False  # 用户显式关闭 → 整条链路 no-op
    if not enabled:
        return ThumbnailCleanerConfig(enabled=False, root=None)

    thumb = thumbnail_config()
    root = (
        xdg_thumbnails_root() if thumb["xdg_enabled"]
        else resolve_thumb_root(thumb)
    )
    return ThumbnailCleanerConfig(
        enabled=True,
        root=root,
        min_age_seconds=conf["orphan_min_age_seconds"],
    )


def load_session_config(
        conf: Optional[SourceTrashServiceConfig] = None,
) -> SessionConfig:
    """会话标记配置。"""
    if conf is None:
        conf = source_trash_config()
    marker_path = conf["session_marker_path"]
    if marker_path:
        path = Path(marker_path)
    else:
        path = platform_cache_root() / "albuswall" / ".session"
    return SessionConfig(
        marker_path=path,
        auto_cleanup_on_dirty=conf["auto_cleanup_on_dirty"],
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
    容器里已有 ``thumbnail_cleaner`` 可用。
    """
    if app is None:
        app = Application.instance()

    session_cfg = load_session_config()
    marker = SessionMarker(session_cfg.marker_path)

    def _on_boot() -> None:
        # 顺序关键：先读，再置脏。反了的话永远读到自己写的 dirty。
        clean = marker.is_clean_shutdown()
        if clean:
            _logger.debug("clean shutdown, skip thumbnail orphan cleanup")
        elif session_cfg.auto_cleanup_on_dirty:
            _logger.info("dirty shutdown, running thumbnail orphan cleanup")
            # noinspection broad-exception
            try:
                container.get("thumbnail_cleaner").scan_and_purge()
            except Exception:
                _logger.exception("thumbnail orphan cleanup failed")
        marker.mark_running()

    def _on_final() -> None:
        marker.mark_clean()

    app.on_boot(_on_boot)
    app.on_final(_on_final)
