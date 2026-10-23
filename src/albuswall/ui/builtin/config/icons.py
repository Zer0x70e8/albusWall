#
"""图标路径解析：把 typed dict 里的图标配置解析成磁盘上的真实文件。

纯函数 + 纯路径运算，不碰 ``Configue`` / ``Resolver``：配置值由
``builtin/config/typed.py`` 取好，这里只负责「偏好 → path.icon →
样式表同级目录」的定位顺序和存在性判断。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from albuswall.log import getLogger
from albuswall.ui.schema import AlbumConfig, WindowConfig

__all__ = [
    "DEFAULT_ALBUM_ICON",
    "DEFAULT_EXTRA_ICON",
    "DEFAULT_SEARCH_ICON",
    "ICON_DIR_CANDIDATES",
    "resolve_icon_dir",
    "resolve_icon_file",
]

_logger = getLogger(__name__)

# 图标默认文件名（同时作为 PreferenceField 的声明默认值）
DEFAULT_ALBUM_ICON = "album_button.svg"
DEFAULT_EXTRA_ICON = "more_button.svg"
DEFAULT_SEARCH_ICON = "search_button.svg"

# 样式表同级目录里图标的候选子目录名
ICON_DIR_CANDIDATES = ("icons", "cover", "imgs", "assets")


def resolve_icon_dir(
        album: AlbumConfig,
        window: WindowConfig,
) -> Optional[Path]:
    """按「偏好 → ``path.icon`` → 样式表同级目录」的顺序定位图标目录。"""
    pref = album["icon_dir_pref"]
    if pref:
        path = Path(str(pref))
        _logger.trace("Icon directory resolved from preference.ui.icon_dir = %s", path)
        return path

    if window["icon_dir"]:
        path = Path(str(window["icon_dir"]))
        _logger.trace("Icon directory resolved from path.icon = %s", path)
        return path

    style_sheet = window["qss"]
    if style_sheet:
        base = Path(str(style_sheet)).parent
        for name in ICON_DIR_CANDIDATES:
            candidate = base / name
            if candidate.is_dir():
                _logger.trace("Icon directory resolved next to the stylesheet: %s", candidate)
                return candidate

    _logger.trace("No icon directory resolved")
    return None


def resolve_icon_file(
        album: AlbumConfig,
        window: WindowConfig,
        raw: Any,
) -> Optional[Path]:
    """把偏好里的图标值（文件名或绝对路径）解析成实际存在的文件。"""
    if not raw:
        return None

    path = Path(str(raw))
    if path.is_absolute():
        if path.is_file():
            _logger.trace("Absolute icon path resolved: %s", path)
            return path
        _logger.debug("Icon file does not exist: %s", path)
        return None

    icon_dir = resolve_icon_dir(album, window)
    if icon_dir is None:
        _logger.debug("No icon directory resolved; cannot locate icon %s", path)
        return None

    full = icon_dir / path
    if full.is_file():
        _logger.trace("Icon path resolved: %s", full)
        return full

    _logger.debug("Icon file does not exist: %s", full)
    return None
