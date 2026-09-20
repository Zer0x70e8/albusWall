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


# noinspection bad-assignment
class WindowPresenterConfs:
    qss_file_name: str = ConfigField("files", "qss", default=None)
    icons: str = ConfigField("files", "icons", default=None)
    qss: Optional[Path | str] = ConfigField("ui", "style_sheet", default=None)
    icon_dir: Optional[Path | str] = ConfigField("path", "cover", default=None)
    preference_config_node: str = ConfigField(
        "ui", "preference_config_node", default="ui.preference")
    preference_file_name: str = ConfigField("files", "preference", default="preference")
    preference_file: Optional[str | Path] = ConfigField("ui", "preference_file", default=None)

    # 总开关：是否允许自动补全主题文件
    auto_complete: bool = ConfigField("ui", "auto_complete_theme", default=True)
    # 是否自动补全 QSS 文件
    auto_complete_qss: bool = ConfigField("ui", "auto_complete_qss", default=True)
    # 是否自动补全图标文件
    auto_complete_icons: bool = ConfigField("ui", "auto_complete_icons", default=True)
    # 仅首次初始化时补全：一旦写入 marker，之后即使缺文件也不再补全
    auto_complete_only_once: bool = ConfigField("ui", "auto_complete_only_once", default=True)

    _MARKER_NAME = ".albuswall_theme_initialized"

    # Built-in theme directory (PosixPath to .../resources/themes/default)
    _BUILTIN_THEME = Path(theme)

    # Filename candidates inside the built-in theme for the QSS, in priority order
    _QSS_SOURCE_NAMES = ("main_window.qss", "qss.qss", "style.qss")

    # ------------------------------------------------------------------ API
    def ensure_theme_files_completed(self, config: Configue) -> None:
        """Make sure the user theme dir mirrors the built-in theme.

        Any file missing from the user's configuration directory is copied
        over from the built-in theme directory. Existing files are left
        untouched so the user can customise them safely.

        为避免用户手动删/改主题文件后又被自动补回，可通过以下 ConfigField
        精细控制行为：
            ui.auto_complete_theme      : 总开关
            ui.auto_complete_qss        : 是否补全 QSS
            ui.auto_complete_icons      : 是否补全图标
            ui.auto_complete_only_once  : 只在首次初始化时补全
        """
        if not self._get(config, "static", "ui", "auto_complete_theme", default=True):
            _logger.debug(
                "auto_complete_theme disabled, skip ensure_theme_files_completed"
            )
            return

        style_sheet = self.resolve_style_sheet(config)
        icon_dir = self._resolve_icon_dir(config, style_sheet)
        marker = self._resolve_marker(style_sheet, icon_dir)

        only_once = self._get(
            config, "static", "ui", "auto_complete_only_once", default=True
        )

        if only_once and marker is not None and marker.exists():
            _logger.debug(
                "Theme already initialized once (marker %s exists), skip auto "
                "completion because auto_complete_only_once is enabled",
                marker,
            )
            return

        _logger.debug(
            "ensure_theme_files_completed: style_sheet=%s, icon_dir=%s, "
            "auto_complete_qss=%s, auto_complete_icons=%s, only_once=%s",
            style_sheet,
            icon_dir,
            self._get(config, "static", "ui", "auto_complete_qss", default=True),
            self._get(config, "static", "ui", "auto_complete_icons", default=True),
            only_once,
        )

        qss_copied = self._ensure_qss(config, style_sheet)
        icons_copied = self._ensure_icons(config, icon_dir)

        if only_once and marker is not None and (qss_copied or icons_copied):
            self._write_marker(marker)

    # --------------------------------------------------------------- sub-ops
    def _ensure_qss(self, config: Configue, style_sheet: Optional[Path]) -> bool:
        if style_sheet is None:
            _logger.debug("QSS target could not be resolved, skip QSS auto completion")
            return False

        if not self._get(config, "static", "ui", "auto_complete_qss", default=True):
            _logger.debug("auto_complete_qss disabled, skip QSS auto completion")
            return False

        try:
            style_sheet.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _logger.warning(
                "Failed to create QSS parent dir %s: %s", style_sheet.parent, exc
            )
            return False

        if style_sheet.exists():
            _logger.debug("QSS already exists, keep user version: %s", style_sheet)
            return False

        sources = [self._BUILTIN_THEME / n for n in self._QSS_SOURCE_NAMES]
        copied = self._copy_first_existing(sources=sources, target=style_sheet)
        if copied:
            _logger.info("Copied QSS from built-in theme to %s", style_sheet)
        else:
            _logger.warning(
                "No QSS source found in built-in theme %s (tried %s); "
                "target %s was not created",
                self._BUILTIN_THEME,
                self._QSS_SOURCE_NAMES,
                style_sheet,
            )
        return copied

    # noinspection bad-argument-type
    def _ensure_icons(self, config: Configue, icon_dir: Optional[Path]) -> bool:
        if icon_dir is None:
            _logger.debug("Icon dir could not be resolved, skip cover auto completion")
            return False

        if not self._get(config, "static", "ui", "auto_complete_icons", default=True):
            _logger.debug("auto_complete_icons disabled, skip cover auto completion")
            return False

        src_icon_dir = self._BUILTIN_THEME / "cover"
        if not src_icon_dir.is_dir():
            _logger.debug(
                "Built-in cover dir %s does not exist, skip cover auto completion",
                src_icon_dir,
            )
            return False

        try:
            icon_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _logger.warning("Failed to create cover dir %s: %s", icon_dir, exc)
            return False

        pattern = self._get(
            config, "static", "files", "icons", default="*"
        ) or "*"

        copied_any = False
        for src in src_icon_dir.glob(pattern):
            if not src.is_file():
                continue

            target = icon_dir / src.name
            if target.exists():
                _logger.debug("Icon already exists, keep user version: %s", target)
                continue

            try:
                shutil.copy2(src, target)
                copied_any = True
                _logger.trace("Copied cover %s -> %s", src.name, target)
            except OSError as exc:
                _logger.warning("Failed to copy cover %s -> %s: %s", src, target, exc)

        return copied_any

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _get(config: Configue, *path, default=None):
        """Safe nested getattr: _get(cfg, 'ui', 'auto_complete_qss', default=True)."""
        obj = config
        for name in path:
            obj = getattr(obj, name, None)
            if obj is None:
                return default
        return obj

    @staticmethod
    def resolve_style_sheet(config: Configue) -> Optional[Path]:
        """Return the full path of the QSS file to ensure exists."""
        if config.static.ui.style_sheet:
            return Path(config.static.ui.style_sheet)
        # Fallback: derive from theme dir + theme name + qss file name
        base = Path(config.static.path.theme) if config.static.path.theme else None
        if base is None:
            return None
        theme_name = config.static.ui.theme or "default"
        qss_name = config.static.files.qss or "qss.qss"
        return base / theme_name / qss_name

    @staticmethod
    def _resolve_icon_dir(config: Configue, style_sheet: Optional[Path]) -> Optional[Path]:
        """Return the directory that should hold the theme icons.

        Priority:
            1. path.cover (absolute, explicit)
            2. <style_sheet.parent>/cover
            3. <path.theme>/<ui.theme>/cover
        """
        if config.static.path.cover:
            return Path(config.static.path.cover)
        if style_sheet is not None:
            return style_sheet.parent / "cover"
        # Last resort: inside the theme root
        base = Path(config.static.path.theme) if config.static.path.theme else None
        if base is None:
            return None
        return base / (config.static.ui.theme or "default") / "cover"

    def _resolve_marker(
            self,
            style_sheet: Optional[Path],
            icon_dir: Optional[Path],
    ) -> Optional[Path]:
        """Marker file used by `auto_complete_only_once`.

        Prefer the QSS parent (that's the per-theme directory); fall back to
        the cover dir's parent; return None if we can't decide.
        """
        if style_sheet is not None:
            return style_sheet.parent / self._MARKER_NAME
        if icon_dir is not None:
            return icon_dir.parent / self._MARKER_NAME
        return None

    @staticmethod
    def _write_marker(marker: Path) -> None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "albuswall theme initialized\n"
                "Delete this file if you want albuswall to auto-complete "
                "missing theme files again.\n",
                encoding="utf-8",
            )
            _logger.debug("Wrote theme initialization marker: %s", marker)
        except OSError as exc:
            _logger.warning("Failed to write theme marker %s: %s", marker, exc)

    @staticmethod
    def _copy_first_existing(sources, target: Path) -> bool:
        for src in sources:
            if src.is_file():
                try:
                    shutil.copy2(src, target)
                except OSError as exc:
                    _logger.warning("Failed to copy %s -> %s: %s", src, target, exc)
                    continue
                _logger.debug("Copied %s -> %s", src, target)
                return True
        return False


# noinspection bad-assignment
class AlbumPresenterConfs:
    window_title: Optional[str] = ConfigField("ui", "window_title", default=None)
