#
"""标题栏 / 专辑相关的用户偏好声明与图标路径解析（兼容层）。

字段用 ``PreferenceField``（而不是 ``ConfigField``）声明，默认值 / 路径由
本模块提供；**这些声明就是 typed dict 的默认值来源**，UI 各层实际取配置走
``builtin/config/typed.py::album_config()`` 返回的 ``AlbumConfig``。

图标解析的实现已抽到 ``.icons``（纯路径运算），本模块的
``icon_dir()`` / ``*_icon_path()`` 保留为兼容入口。

说明：``dynamic.preference``（见 ``config/setup.py``）目前还没接到
``PreferenceField`` 的 ``__get__`` 上，因此这里仍是「声明 + 静态解析」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .icons import (
    DEFAULT_ALBUM_ICON,
    DEFAULT_EXTRA_ICON,
    DEFAULT_SEARCH_ICON,
    resolve_icon_dir,
    resolve_icon_file,
)
from .registory import PreferenceField


# noinspection bad-assignment
class PreferenceConfig:
    """标题栏 / 专辑相关的统一用户偏好配置（声明 + 兼容访问）。"""

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
        "icons", "album_icon", default=DEFAULT_ALBUM_ICON)
    extra_icon: Optional[str | Path] = PreferenceField(
        "icons", "extra_icon", default=DEFAULT_EXTRA_ICON)
    search_icon: Optional[str | Path] = PreferenceField(
        "icons", "search_icon", default=DEFAULT_SEARCH_ICON)

    # ---------------- 专辑 / 标题栏文案 ----------------
    default_album_uuid: Optional[str] = PreferenceField(
        "album", "default_album_uuid", default=None)
    search_expanded: bool = PreferenceField(
        "album", "search_expanded", default=False)
    album_text: str = PreferenceField("album", "album_text", default="")
    extra_text: str = PreferenceField("album", "extra_text", default="")
    search_placeholder: str = PreferenceField(
        "album", "search_text", default="")

    # ---------------- 图标路径解析（兼容入口） ----------------

    @classmethod
    def _typed(cls):
        """取一份 typed dict 快照（延迟 import，避免与本模块循环依赖）。"""
        from .typed import album_config, window_config

        return album_config(), window_config()

    @classmethod
    def icon_dir(cls) -> Optional[Path]:
        """按「偏好 → path.icon → 样式表同级目录」的顺序定位图标目录。"""
        album, window = cls._typed()
        return resolve_icon_dir(album, window)

    @classmethod
    def _resolve(cls, raw: Any) -> Optional[Path]:
        """把偏好里的图标值解析成实际存在的文件路径。"""
        album, window = cls._typed()
        return resolve_icon_file(album, window, raw)

    def album_icon_path(self) -> Optional[Path]:
        return self._resolve(self.album_icon)

    def extra_icon_path(self) -> Optional[Path]:
        return self._resolve(self.extra_icon)

    def search_icon_path(self) -> Optional[Path]:
        return self._resolve(self.search_icon)
