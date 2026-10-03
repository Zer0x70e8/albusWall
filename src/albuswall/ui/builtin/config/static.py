#
""""""

import shutil
from pathlib import Path
from logging import getLogger
from typing import Optional

from albuswall.log import TRACE, Logger
from albuswall.configue import ConfigField, Configue
from albuswall.resources import theme

_logger: Logger = getLogger(".".join(str(__name__).split(".")[:-1]))  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


class WindowPresenterConfs:
    qss_file_name: Optional[str] = ConfigField("files", "qss", default=None)
    icons: Optional[str] = ConfigField("files", "icons", default=None)
    qss: Optional[Path | str] = ConfigField("ui", "style_sheet", default=None)
    icon_dir: Optional[Path | str] = ConfigField("path", "icon", default=None)
    preference_config_node: str = ConfigField(
        "ui", "preference_config_node", default="ui.preference")
    preference_file_name: str = ConfigField("files", "preference", default="preference")
    preference_file: Optional[str | Path] = ConfigField("ui", "preference_file", default=None)
    window_state_file_name: str = ConfigField("files", "window_state", default="window.json")
    # window_state_file: Path | str = ConfigField(
    #     "ui", "window_states_file", default="${files:window_state}/window.json")

    auto_complete: bool = ConfigField("ui", "auto_complete_theme", default=True)
    auto_complete_qss: bool = ConfigField("ui", "auto_complete_qss", default=True)
    auto_complete_icons: bool = ConfigField("ui", "auto_complete_icons", default=True)
    auto_complete_only_once: bool = ConfigField("ui", "auto_complete_only_once", default=True)

    _MARKER_NAME = ".albuswall_theme_initialized"
    _BUILTIN_THEME = Path(theme)
    _QSS_SOURCE_NAMES = ("main_window.qss", "qss.qss", "style.qss")
    _ICON_SOURCE_DIR_NAME = "icon"

    # ------------------------------------------------------------------ API
    def ensure_theme_files_completed(self, config: Configue) -> None:
        """按配置把内置主题里缺失的文件补到用户主题目录。

        路径全部由 _config.ini 插值算好：
            ui.style_sheet  -> 目标 QSS
            path.icon       -> 目标图标目录
        通过以下开关精细控制：
            ui.auto_complete_theme / auto_complete_qss / auto_complete_icons
            ui.auto_complete_only_once
        """
        if not config.static.ui.auto_complete_theme:
            _logger.debug("auto_complete_theme disabled, skip")
            return

        style_sheet = Path(config.static.ui.style_sheet) \
            if config.static.ui.style_sheet else None
        icon_dir = Path(config.static.path.icon) \
            if config.static.path.icon else None

        # marker 放在主题目录下（QSS 所在目录）
        marker = (style_sheet or icon_dir)
        marker = marker.parent / self._MARKER_NAME if marker else None
        only_once = config.static.ui.auto_complete_only_once

        if only_once and marker and marker.exists():
            _logger.debug("Theme already initialized (marker %s), skip", marker)
            return

        qss_copied = self._ensure_qss(config, style_sheet)
        icons_copied = self._ensure_icons(config, icon_dir)

        if only_once and marker and (qss_copied or icons_copied):
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    "albuswall theme initialized\n"
                    "Delete this file to re-enable auto completion.\n",
                    encoding="utf-8",
                )
                _logger.debug("Wrote theme marker: %s", marker)
            except OSError as exc:
                _logger.warning("Failed to write marker %s: %s", marker, exc)

    # --------------------------------------------------------------- sub-ops
    def _ensure_qss(self, config: Configue, target: Optional[Path]) -> bool:
        if target is None or not config.static.ui.auto_complete_qss:
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

    def _ensure_icons(self, config: Configue, icon_dir: Optional[Path]) -> bool:
        if icon_dir is None or not config.static.ui.auto_complete_icons:
            return False
        src_dir = self._BUILTIN_THEME / self._ICON_SOURCE_DIR_NAME
        if not src_dir.is_dir():
            return False
        icon_dir.mkdir(parents=True, exist_ok=True)
        pattern = config.static.files.icons or "*"
        copied = False
        for src in src_dir.glob(pattern):
            target = icon_dir / src.name
            if src.is_file() and not target.exists():
                shutil.copy2(src, target)
                copied = True
                _logger.trace("Copied icon %s -> %s", src.name, target)
        return copied


# noinspection bad-assignment
class AlbumPresenterConfs:
    window_title: Optional[str] = ConfigField("ui", "window_title", default=None)
