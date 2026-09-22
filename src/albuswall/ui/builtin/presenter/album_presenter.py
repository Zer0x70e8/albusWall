#
""""""

from __future__ import annotations

from uuid import UUID
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import albuswall
from albuswall.log import TRACE, Logger
from albuswall.dto.album import Album
from albuswall.ui.vo.album import TitleBarVO

from ..config.registory import PreferenceField
from ..config.static import WindowPresenterConfs

if TYPE_CHECKING:
    from albuswall.services.view import ViewService
    from ..window.titlebar import TitleBar

_DEFAULT_TITLE = albuswall.__title__

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)

# 图标默认文件名
_DEFAULT_ALBUM_ICON = "album_button.svg"
_DEFAULT_EXTRA_ICON = "more_button.svg"
_DEFAULT_SEARCH_ICON = "search_button.svg"

_ICON_DIR_CANDIDATES = ("icons", "cover", "imgs", "assets")


# noinspection bad-assignment
class PreferenceConfig:
    """标题栏 / 专辑相关的统一用户偏好配置。

    合并了原先分散在 static / dynamic 中的多个配置类：

        WindowPresenterPreferences.theme        -> theme
        WindowPresenterPreferences.theme_dir    -> theme_dir
        WindowPresenterPreferences.style_sheet  -> style_sheet
        WindowPresenterPreferences.icon_dir     -> icon_dir_pref
        WindowPresenterPreferences.window_title -> window_title
        IconPreferences.album_icon              -> album_icon
        IconPreferences.extra_icon              -> extra_icon
        IconPreferences.search_icon             -> search_icon
        AlbumPresenterPreferences.search_expanded -> search_expanded
        AlbumPresenterPreferences.album_text    -> album_text
        AlbumPresenterPreferences.extra_text    -> extra_text
        ...search_text                   -> search_text
        GeneralPreferences.language             -> language

    读取位置：``dynamic.preference.<section>.<name>``，与
    ``PreferenceField("section", "name")`` 一致。

    说明：原来 ``AlbumPresenterConfs`` / ``WindowPresenterConfs`` 里的
    标题栏文案（``album_text`` / ``extra_text`` / ``search_text``）
    现已并入此类，字段本身走偏好配置；如果 preference.ini 里没有对应项，
    就会使用各自 ``default``。
    """

    # ---------------- ui / 主题 ----------------
    theme: str = PreferenceField("ui", "theme", default="default")
    theme_dir: Optional[str | Path] = PreferenceField(
        "ui", "theme_dir", default=None)
    style_sheet: Optional[str | Path] = PreferenceField(
        "ui", "style_sheet", default=None)
    # 注意：原 IconPreferences 里的 icon_dir() 是方法，这里字段改名避免冲突
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

    # noinspection string-conversion-without-dunder-method
    @classmethod
    def icon_dir(cls) -> Optional[Path]:
        """解析图标根目录，返回第一个实际存在的候选；都拿不到返回 None。

        优先级：
          1) preference.ui.icon_dir
          2) static.path.cover（嵌套）
          3) 扁平命名的 static.icon_dir / static.cover
          4) <style_sheet>.parent / _ICON_DIR_CANDIDATES 中第一个存在的
        """
        # 1) 用户偏好
        pref = getattr(cls(), "icon_dir_pref", None)
        if pref:
            p = Path(str(pref))
            _logger.trace("图标目录命中 preference.ui.icon_dir = %s", p)
            return p

        # 2) static 配置（回退）
        static = WindowPresenterConfs()

        path_obj = getattr(static, "path", None)
        if path_obj is not None:
            static_icon = getattr(path_obj, "cover", None)
            if static_icon:
                p = Path(str(static_icon))
                _logger.trace("图标目录命中 static.path.cover = %s", p)
                return p

        for attr in ("icon_dir", "cover"):
            v = getattr(static, attr, None)
            if v:
                p = Path(str(v))
                _logger.trace("图标目录命中 static.%s = %s", attr, p)
                return p

        ui_obj = getattr(static, "ui", None)
        style_sheet = (
            getattr(ui_obj, "style_sheet", None) if ui_obj is not None else None
        )
        if not style_sheet:
            style_sheet = getattr(static, "qss", None)
        if style_sheet:
            base = Path(str(style_sheet)).parent
            for name in _ICON_DIR_CANDIDATES:
                candidate = base / name
                if candidate.is_dir():
                    _logger.trace("图标目录命中样式表同级 %s", candidate)
                    return candidate
                else:
                    _logger.trace("未解析到图标目录: %s", candidate)
        _logger.trace("未解析到图标目录")
        return None

    @classmethod
    def _resolve(cls, raw: Any) -> Optional[Path]:
        """把单个偏好值解析成**存在的**绝对路径；不存在返回 None。"""
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

    # ---------------- 对外接口 ----------------

    def album_icon_path(self) -> Optional[Path]:
        """专辑封面图标路径；不存在返回 None。"""
        return self._resolve(self.album_icon)

    def extra_icon_path(self) -> Optional[Path]:
        """更多/附加按钮图标路径；不存在返回 None。"""
        return self._resolve(self.extra_icon)

    def search_icon_path(self) -> Optional[Path]:
        """搜索按钮图标路径；不存在返回 None。"""
        return self._resolve(self.search_icon)


class AlbumPresenter:
    """负责将 Album DTO 的数据呈现在标题栏上。"""

    def __init__(
            self,
            title_bar: "TitleBar",
            view_service: "ViewService"
    ):
        self._title_bar = title_bar
        self._view_service = view_service
        self._config = PreferenceConfig()
        self._current_album: Album | None = None

    # ---------------- 对外接口 ----------------
    # noinspection unused-parameter
    def setup(self, container):
        self._load_default_album()
        self.refresh()
        _logger.debug(
            "view_service=%r module=%s",
            self._view_service, type(self._view_service).__module__)

        # if container.get("config").static.debug:
        #     _logger.debug(self)

    def refresh(self) -> None:
        self._apply_window_settings()
        self._title_bar.set_vo(self._build_vo(self._current_album))

    def set_album(self, album: Album) -> None:
        self._current_album = album
        self._title_bar.set_vo(self._build_vo(album))

    def _load_default_album(self) -> None:
        """决定初始展示的专辑。

        解析顺序：
          1. 偏好 ``album.default_album_uuid``；
          2. 缺失时取 ``ViewService.get_active_album_uuids()`` 的第一个；
          3. 都没有则保持 ``None``（退化为空标题栏）。
        """
        album_uuid: UUID | str | None = self._config.default_album_uuid

        if not album_uuid:
            active = self._view_service.get_active_album_uuids()
            _logger.debug("presenter active = %r", active)
            album_uuid = active[0] if active else None

        if album_uuid is None:
            self._current_album = None
            _logger.debug("未解析到默认专辑，标题栏将以空专辑渲染")
            return

        self._current_album = self._view_service.get_album_by_uuid(album_uuid)
        if self._current_album is None:
            _logger.debug("默认专辑 %s 查询为空", album_uuid)

    def _window_title(self) -> str:
        return self._config.window_title or _DEFAULT_TITLE

    def _apply_window_settings(self) -> None:
        self._title_bar.window().setWindowTitle(self._window_title())

    @staticmethod
    def _text_or_none(value: Optional[str]) -> Optional[str]:
        """空串统一转为 None：渲染层据此跳过文本，避免覆盖图标。"""
        if value is None:
            return None
        s = str(value)
        return s if s != "" else None

    def _album_cover_path(self, album: Album | None) -> Optional[Path]:
        """组合调用 ViewService，得到专辑封面的完整磁盘路径。"""
        if album is None:
            return None
        return self._view_service.get_cover_full_path(album)

    def _build_vo(self, album: Album | None) -> TitleBarVO:
        """由 PreferenceConfig + Album DTO 构造 TitleBarVO。"""
        return TitleBarVO(
            window_title=self._window_title(),

            # 封面：交给渲染层决定如何展示（例如 icon_label / 背景）。
            cover=self._album_cover_path(album),

            title=album.title if album is not None else None,
            description=album.description if album is not None else None,

            # 三个按钮图标
            album_icon=self._config.album_icon_path(),
            extra_icon=self._config.extra_icon_path(),
            search_icon=self._config.search_icon_path(),

            # 三个按钮文本
            album_text=self._text_or_none(self._config.album_text),
            extra_text=self._text_or_none(self._config.extra_text),
            search_text=self._text_or_none(
                self._config.search_placeholder),

            search_expanded=self._config.search_expanded,
        )

    # noinspection GrazieInspection
    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__} (",
            f"\tcurrent: UUID({self._current_album.uuid if self._current_album else 'None'})",
            # It's too lang.
            # "\tview_service: ",
            # *[f"\t\t{l}" for l in str(self._view_service).splitlines()],
            ")"
        ))
