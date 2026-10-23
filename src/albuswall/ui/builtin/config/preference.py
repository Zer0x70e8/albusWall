#
"""标题栏 / 专辑相关的用户偏好声明与图标路径解析。

历史原因，这批字段用 ``PreferenceField``（而不是 ``ConfigField``）声明，
读取走 ``Resolver``；解析顺序与原先写在 ``album_presenter`` 里时一致，
只是把「配置声明 + 路径解析」从 presenter 层挪回 config 层。

说明：``dynamic.preference``（见 ``config/setup.py``）目前还没接到
``PreferenceField`` 的 ``__get__`` 上，因此这里仍是「声明 + 静态解析」，
不要在这里改读 ``dynamic.preference``，否则取值来源会变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from albuswall.log import getLogger

from .registory import PreferenceField
from .static import WindowPresenterConfs

_logger = getLogger(__name__)

# 图标默认文件名
_DEFAULT_ALBUM_ICON = "album_button.svg"
_DEFAULT_EXTRA_ICON = "more_button.svg"
_DEFAULT_SEARCH_ICON = "search_button.svg"

# 样式表同级目录里图标的候选子目录名
_ICON_DIR_CANDIDATES = ("icons", "cover", "imgs", "assets")


# noinspection bad-assignment
class PreferenceConfig:
    """标题栏 / 专辑相关的统一用户偏好配置。"""

    # ---------------- ui / 主题 ----------------
    theme: str = PreferenceField("ui", "theme", default="default")
    theme_dir: Optional[str | Path] = PreferenceField(
        "ui", "theme_dir", default=None)
    style_sheet: Optional[str | Path] = PreferenceField(
        "ui", "style_sheet", default=None)
    icon_dir_pref: Optional[str | Path] = PreferenceField(
        "ui", "icon_dir", default=None)
    window_title: str = PreferenceField(
        "ui", "window_title", default="AlbusWall")

    # ---------------- 图标文件 ----------------
    album_icon: Optional[str | Path] = PreferenceField(
        "icons", "album_icon", default=_DEFAULT_ALBUM_ICON)
    extra_icon: Optional[str | Path] = PreferenceField(
        "icons", "extra_icon", default=_DEFAULT_EXTRA_ICON)
    search_icon: Optional[str | Path] = PreferenceField(
        "icons", "search_icon", default=_DEFAULT_SEARCH_ICON)

    # ---------------- 专辑 / 标题栏文案 ----------------
    default_album_uuid: Optional[str] = PreferenceField(
        "album", "default_album_uuid", default=None)
    search_expanded: bool = PreferenceField(
        "album", "search_expanded", default=False)
    album_text: str = PreferenceField("album", "album_text", default="")
    extra_text: str = PreferenceField("album", "extra_text", default="")
    search_placeholder: str = PreferenceField(
        "album", "search_text", default="")

    # ---------------- 通用 ----------------
    language: str = PreferenceField(
        "general", "language", default="en_US")

    # ---------------- 图标路径解析 ----------------

    @classmethod
    def icon_dir(cls) -> Optional[Path]:
        """按「偏好 → path.icon → 样式表同级目录」的顺序定位图标目录。"""
        pref = getattr(cls(), "icon_dir_pref", None)
        if pref:
            p = Path(str(pref))
            _logger.trace("图标目录命中 preference.ui.icon_dir = %s", p)
            return p

        static = WindowPresenterConfs()
        if static.icon_dir:
            p = Path(str(static.icon_dir))
            _logger.trace("图标目录命中 path.icon = %s", p)
            return p

        style_sheet = static.qss
        if style_sheet:
            base = Path(str(style_sheet)).parent
            for name in _ICON_DIR_CANDIDATES:
                candidate = base / name
                if candidate.is_dir():
                    _logger.trace("图标目录命中样式表同级 %s", candidate)
                    return candidate

        _logger.trace("未解析到图标目录")
        return None

    @classmethod
    def _resolve(cls, raw: Any) -> Optional[Path]:
        """把偏好里的图标值解析成实际存在的文件路径。"""
        if not raw:
            return None

        p = Path(str(raw))
        if p.is_absolute():
            if p.is_file():
                _logger.trace("图标绝对路径命中: %s", p)
                return p
            _logger.debug("图标文件不存在: %s", p)
            return None

        d = cls.icon_dir()
        if d is None:
            _logger.debug("未解析到图标目录，无法定位图标 %s", p)
            return None

        full = d / p
        if full.is_file():
            _logger.trace("图标路径命中: %s", full)
            return full

        _logger.debug("图标文件不存在: %s", full)
        return None

    def album_icon_path(self) -> Optional[Path]:
        return self._resolve(self.album_icon)

    def extra_icon_path(self) -> Optional[Path]:
        return self._resolve(self.extra_icon)

    def search_icon_path(self) -> Optional[Path]:
        return self._resolve(self.search_icon)
