#
"""UI 配置的 typed dict 加载器 + 由配置驱动的主题补全。

所有取值都来自**静态配置声明**（``.static`` / ``.preference`` 里的
``ConfigField`` / ``PathField``），本模块只做两件事：

1. 把声明映射成 ``albuswall.ui.schema`` 的 typed dict（``*_config()``）；
2. 按 typed dict 做「内置主题 → 用户主题目录」的文件补全
   （``ensure_theme_files_completed``），原来挂在
   ``WindowPresenterConfs.ensure_theme_files_completed`` 上。

import 本模块会顺带 import ``.static`` / ``.preference``，确保
``ConfigField.__set_name__`` 的注册先于 ``ConfigDeclaration.load`` 跑过。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, cast

from albuswall.configue.declaration import ConfigDeclaration
from albuswall.configue.typed import load_typed
from albuswall.log import getLogger
from albuswall.resources import theme
from albuswall.ui.schema import (
    AlbumConfig,
    PreferenceSourceConfig,
    ThumbnailGridConfig,
    WindowConfig,
)

# noqa: F401  —— import 即注册（ConfigField.__set_name__ 写进声明注册表）
from . import preference as _preference  # noqa: F401
from . import static as _static  # noqa: F401
from .icons import resolve_icon_dir, resolve_icon_file
from .registory import PreferenceDeclaration

__all__ = [
    "album_config",
    "ensure_theme_files_completed",
    "preference_source_config",
    "resolve_icon_dir",
    "resolve_icon_file",
    "thumbnail_grid_config",
    "window_config",
]

_logger = getLogger("albuswall.ui.config")

# 内置主题目录（源）与自动补全常量
_BUILTIN_THEME = Path(theme)
_MARKER_NAME = ".albuswall_theme_initialized"
_QSS_SOURCE_NAMES = ("main_window.qss", "qss.qss", "style.qss")
_ICON_SOURCE_DIR_NAME = "icon"
_MARKER_CONTENT = (
    "albuswall theme initialized\n"
    "Delete this file to re-enable auto completion.\n"
)


# ══════════════════════════════════════════════════
# typed dict 快照
# ══════════════════════════════════════════════════
def window_config() -> WindowConfig:
    """窗口 / 主题相关的静态配置快照。"""
    return cast(WindowConfig, load_typed(WindowConfig, ConfigDeclaration))


def album_config() -> AlbumConfig:
    """标题栏 / 专辑偏好快照：偏好层声明优先，其余回落到静态字段。"""
    return cast(
        AlbumConfig,
        load_typed(AlbumConfig, PreferenceDeclaration, ConfigDeclaration),
    )


def thumbnail_grid_config() -> ThumbnailGridConfig:
    """缩略图网格的静态配置快照。"""
    return cast(
        ThumbnailGridConfig,
        load_typed(ThumbnailGridConfig, ConfigDeclaration),
    )


def preference_source_config() -> PreferenceSourceConfig:
    """preference.ini 的落点快照（``ui.preference_file`` / data 目录）。"""
    return cast(
        PreferenceSourceConfig,
        load_typed(PreferenceSourceConfig, ConfigDeclaration),
    )


# ══════════════════════════════════════════════════
# 主题补全
# ══════════════════════════════════════════════════
def ensure_theme_files_completed(conf: Optional[WindowConfig] = None) -> None:
    """按配置把内置主题里缺失的文件补到用户主题目录。

    路径全部由静态配置算好：
        ``ui.style_sheet``  -> 目标 QSS（``PathField`` 保证是 Path | None）
        ``path.icon``       -> 目标图标目录（同上）
    通过以下开关精细控制：
        ``ui.auto_complete_theme`` / ``auto_complete_qss`` / ``auto_complete_icons``
        ``ui.auto_complete_only_once``
    """
    if conf is None:
        conf = window_config()

    if not conf["auto_complete"]:
        _logger.debug("auto_complete_theme disabled, skip")
        return

    style_sheet = conf["qss"]
    icon_dir = conf["icon_dir"]

    # marker 跟随主题目录（即 style_sheet 所在目录）
    marker: Optional[Path] = (
        style_sheet.parent / _MARKER_NAME if style_sheet else None
    )
    only_once = conf["auto_complete_only_once"]

    if only_once and marker and marker.exists():
        _logger.debug("Theme already initialized (marker %s), skip", marker)
        return

    qss_copied = _ensure_qss(style_sheet) if conf["auto_complete_qss"] else False
    icons_copied = _ensure_icons(icon_dir) if conf["auto_complete_icons"] else False

    if only_once and marker and (qss_copied or icons_copied):
        _write_marker(marker)


def _ensure_qss(target: Optional[Path]) -> bool:
    if target is None:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _logger.debug("QSS exists, keep user version: %s", target)
        return False
    for name in _QSS_SOURCE_NAMES:
        src = _BUILTIN_THEME / name
        if src.is_file():
            shutil.copy2(src, target)
            _logger.info("Copied QSS %s -> %s", src, target)
            return True
    _logger.warning("No QSS source in built-in theme %s", _BUILTIN_THEME)
    return False


def _ensure_icons(icon_dir: Optional[Path]) -> bool:
    if icon_dir is None:
        return False
    src_dir = _BUILTIN_THEME / _ICON_SOURCE_DIR_NAME
    if not src_dir.is_dir():
        return False
    icon_dir.mkdir(parents=True, exist_ok=True)
    copied = False
    for src in src_dir.glob("*"):
        target = icon_dir / src.name
        if src.is_file() and not target.exists():
            shutil.copy2(src, target)
            copied = True
            _logger.trace("Copied icon %s -> %s", src.name, target)
    return copied


def _write_marker(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_MARKER_CONTENT, encoding="utf-8")
        _logger.debug("Wrote theme marker: %s", marker)
    except OSError as exc:
        _logger.warning("Failed to write marker %s: %s", marker, exc)
