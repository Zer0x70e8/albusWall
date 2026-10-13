#
""""""

import shutil
from pathlib import Path
from typing import Optional

from albuswall.log import getLogger
from albuswall.configue import ConfigField, PathField, Configue
from albuswall.resources import theme

_logger = getLogger(__package__)


class WindowPresenterConfs:
    # 让 files.qss 有真实默认值，ui.style_sheet 通过插值解析到它
    qss_file_name: str = ConfigField("files", "qss", default="main_window.qss")
    qss: Optional[Path] = PathField(
        "ui", "style_sheet", default="${path:theme}/${files:qss}")
    icon_dir: Optional[Path] = PathField("path", "icon", default=None)
    preference_config_node: str = ConfigField(
        "ui", "preference_config_node", default="ui.preference")
    preference_file_name: str = ConfigField("files", "preference", default="preference")
    preference_file: Optional[Path] = PathField("ui", "preference_file", default=None)
    window_state_file_name: str = ConfigField("files", "window_state", default="window.json")

    auto_complete: bool = ConfigField("ui", "auto_complete_theme", default=True)
    auto_complete_qss: bool = ConfigField("ui", "auto_complete_qss", default=True)
    auto_complete_icons: bool = ConfigField("ui", "auto_complete_icons", default=True)
    auto_complete_only_once: bool = ConfigField("ui", "auto_complete_only_once", default=True)

    _MARKER_NAME = ".albuswall_theme_initialized"
    _BUILTIN_THEME = Path(theme)
    _QSS_SOURCE_NAMES = ("main_window.qss", "qss.qss", "style.qss")
    _ICON_SOURCE_DIR_NAME = "icon"
    _MARKER_CONTENT = (
        "albuswall theme initialized\n"
        "Delete this file to re-enable auto completion.\n"
    )

    # API
    def ensure_theme_files_completed(self, config: Configue) -> None:
        """按配置把内置主题里缺失的文件补到用户主题目录。

        路径全部由 _config.ini 插值算好：
            ui.style_sheet  -> 目标 QSS（PathField 保证是 Path | None）
            path.icon       -> 目标图标目录（同上）
        通过以下开关精细控制：
            ui.auto_complete_theme / auto_complete_qss / auto_complete_icons
            ui.auto_complete_only_once
        """
        ui = config.static.ui

        if not ui.auto_complete_theme:
            _logger.debug("auto_complete_theme disabled, skip")
            return

        style_sheet = ui.style_sheet
        icon_dir = config.static.path.icon

        # marker 跟随主题目录（即 style_sheet 所在目录）
        marker: Optional[Path] = (
            style_sheet.parent / self._MARKER_NAME if style_sheet else None
        )
        only_once = ui.auto_complete_only_once

        if only_once and marker and marker.exists():
            _logger.debug("Theme already initialized (marker %s), skip", marker)
            return

        qss_copied = self._ensure_qss(style_sheet) if ui.auto_complete_qss else False
        icons_copied = self._ensure_icons(icon_dir) if ui.auto_complete_icons else False

        if only_once and marker and (qss_copied or icons_copied):
            self._write_marker(marker)

    # sub-ops
    def _ensure_qss(self, target: Optional[Path]) -> bool:
        if target is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _logger.debug("QSS exists, keep user version: %s", target)
            return False
        for name in self._QSS_SOURCE_NAMES:
            src = self._BUILTIN_THEME / name
            if src.is_file():
                shutil.copy2(src, target)
                _logger.info("Copied QSS %s -> %s", src, target)
                return True
        _logger.warning("No QSS source in built-in theme %s", self._BUILTIN_THEME)
        return False

    def _ensure_icons(self, icon_dir: Optional[Path]) -> bool:
        if icon_dir is None:
            return False
        src_dir = self._BUILTIN_THEME / self._ICON_SOURCE_DIR_NAME
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

    def _write_marker(self, marker: Path) -> None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(self._MARKER_CONTENT, encoding="utf-8")
            _logger.debug("Wrote theme marker: %s", marker)
        except OSError as exc:
            _logger.warning("Failed to write marker %s: %s", marker, exc)


class AlbumPresenterConfs:
    window_title: Optional[str] = ConfigField("ui", "window_title", default=None)


class ThumbnailGridPresenterConfs:
    favorite_on_icon: Optional[Path] = PathField(
        "ui", "favorite_on_icon", default="${path:icon}/favorite_icon.svg")
