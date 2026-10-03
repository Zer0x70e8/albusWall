#
""""""

from __future__ import annotations

from uuid import UUID
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import (
    QObject, QRunnable, QThreadPool, Qt, Signal, Slot,
    QModelIndex, QMetaObject, Q_ARG,
)
from PySide6.QtGui import QPixmap

import albuswall
from albuswall.log import TRACE, Logger
from albuswall.dto.album import Album
from albuswall.ui.vo.album import TitleBarVO

from ..config.registory import PreferenceField
from ..config.static import WindowPresenterConfs
from ..model.album_model import AlbumModel, AlbumRole
from ..delegate.album_cover_delegate import AlbumCOverDelegate
from ..widgets.square_grid import SquareGridView

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


class _CoverJob(QRunnable):
    """把一个可调用对象丢进线程池；异常只记日志，不传播。"""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        except Exception:  # noqa: BLE001
            _logger.exception("封面加载任务异常")


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
        pref = getattr(cls(), "icon_dir_pref", None)
        if pref:
            p = Path(str(pref))
            _logger.trace("图标目录命中 preference.ui.icon_dir = %s", p)
            return p

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


class AlbumPresenter(QObject):
    """专辑面板 presenter。

    同时管两件事，状态只有一份（`_current_album`）：

    - 网格列表：从 ``ViewService`` 拉专辑 → 喂给 ``SquareGridView``；
    - 标题栏：把当前专辑 + 偏好配置 → 打包成 ``TitleBarVO`` 交给 ``TitleBar``。

    两侧通过同一个 ``_current_album`` 保持一致：

    - 列表点击 → ``set_album`` → 刷 TitleBar + 高亮网格
    - 外部 ``set_album`` → 刷 TitleBar + 高亮网格

    依赖 ``AlbumModel`` 至少提供以下接口::

        append(uuid: str, pixmap: QPixmap | None = None) -> int
        clear() -> None
        rowCount() -> int
        uuid_at(row: int) -> str
        setPixmap(row: int, pixmap: QPixmap) -> None
        index(row: int, column: int) -> QModelIndex   # QAbstractItemModel 自带
        data(index: QModelIndex, role: int) -> Any
    """

    album_changed = Signal(Album)

    def __init__(
            self,
            title_bar: "TitleBar",
            album_view: SquareGridView,
            view_service: "ViewService",
            parent=None
    ):
        super().__init__(parent)
        self._title_bar = title_bar
        self._view = album_view
        self._view_service = view_service
        self._config = PreferenceConfig()
        self._current_album: Album | None = None
        self._pool = QThreadPool.globalInstance()

        # --- 注入 model / delegate（SquareGridView 是被动的，得我们提供）---
        self._model = AlbumModel(self)
        self._delegate = AlbumCOverDelegate(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self._view.setSelectionMode(
            self._view.SelectionMode.SingleSelection)
        self._view.clicked.connect(self._on_album_clicked)

    @property
    def current(self) -> Album | None:
        return self._current_album

    # ---------------- 生命周期 ----------------

    def setup(self, container) -> None:
        self._populate_list()
        self._load_default_album()
        self.refresh()
        self.album_changed.emit(self._current_album)
        _logger.debug(
            "view_service=%r module=%s",
            self._view_service, type(self._view_service).__module__)

    def teardown(self):...

    def refresh(self) -> None:
        """重绘 TitleBar + 重新同步网格选中。"""
        self._apply_window_settings()
        self._title_bar.set_vo(self._build_vo(self._current_album))
        self._sync_selection(self._current_album)

    def set_album(self, album: Album) -> None:
        """切换当前专辑：状态、TitleBar、网格高亮一起变。"""
        self._current_album = album
        self._title_bar.set_vo(self._build_vo(album))
        self._sync_selection(album)
        self.album_changed.emit(album)

    # ---------------- 网格数据 ----------------

    def _populate_list(self) -> None:
        """拉活动专辑列表，先塞占位，再异步回填封面。"""
        self._model.clear()
        try:
            uuids = self._view_service.get_active_album_uuids()
        except Exception:  # noqa: BLE001
            _logger.exception("拉取活动专辑列表失败")
            return

        _logger.debug("活动专辑: %r", uuids)
        for u in uuids:
            self._model.append(str(u))
        for u in uuids:
            self._load_cover_async(u)

    def _load_cover_async(self, album_uuid: UUID | str) -> None:
        """在后台把封面读成 QPixmap，回主线程按 uuid 回填。"""

        def job() -> None:
            album = self._view_service.get_album_by_uuid(album_uuid)
            path = (
                self._view_service.get_cover_full_path(album)
                if album is not None else None
            )
            pm = QPixmap(str(path)) if path else QPixmap()
            QMetaObject.invokeMethod(
                self, "_on_cover_ready",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, str(album_uuid)),
                Q_ARG(QPixmap, pm),
            )

        self._pool.start(_CoverJob(job))

    @Slot(str, QPixmap)
    def _on_cover_ready(self, uuid_str: str, pixmap: QPixmap) -> None:
        """线程池回调：按 uuid 定位行，避免列表变动后 row 错位。"""
        row = self._row_for_uuid(uuid_str)
        if row < 0:
            _logger.trace("封面回填时找不到 uuid=%s（可能已被移除）", uuid_str)
            return
        self._model.setPixmap(row, pixmap)

    def _row_for_uuid(self, uuid_str: str) -> int:
        for r in range(self._model.rowCount()):
            if self._model.uuid_at(r) == uuid_str:
                return r
        return -1

    # ---------------- 交互 ----------------

    @Slot(QModelIndex)
    def _on_album_clicked(self, index: QModelIndex) -> None:
        uuid_val = index.data(AlbumRole.Uuid)
        if not uuid_val:
            return
        album = self._view_service.get_album_by_uuid(uuid_val)
        if album is None:
            _logger.debug("点击的专辑 %s 查询为空", uuid_val)
            return
        self.set_album(album)

    def _sync_selection(self, album: Album | None) -> None:
        """让网格高亮/滚动到当前专辑。"""
        if album is None:
            self._view.clearSelection()
            return
        row = self._row_for_uuid(str(album.uuid))
        if row < 0:
            return
        idx = self._model.index(row, 0)
        self._view.setCurrentIndex(idx)
        self._view.scrollTo(idx)

    # ---------------- 默认专辑 ----------------

    def _load_default_album(self) -> None:
        """解析顺序：
            1. 偏好 ``album.default_album_uuid``；
            2. 缺失时取 ``ViewService.get_active_album_uuids()`` 的第一个；
            3. 都没有则保持 ``None``。
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

    # ---------------- TitleBar VO ----------------

    def _window_title(self) -> str:
        return self._config.window_title or _DEFAULT_TITLE

    def _apply_window_settings(self) -> None:
        self._title_bar.window().setWindowTitle(self._window_title())

    @staticmethod
    def _text_or_none(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        s = str(value)
        return s if s != "" else None

    def _album_cover_path(self, album: Album | None) -> Optional[str]:
        if album is None:
            return None
        return self._view_service.get_cover_full_path(album)

    def _build_vo(self, album: Album | None) -> TitleBarVO:
        return TitleBarVO(
            window_title=self._window_title(),
            cover=self._album_cover_path(album),
            title=album.title if album is not None else None,
            description=album.description if album is not None else None,
            album_icon=self._config.album_icon_path(),
            extra_icon=self._config.extra_icon_path(),
            search_icon=self._config.search_icon_path(),
            album_text=self._text_or_none(self._config.album_text),
            extra_text=self._text_or_none(self._config.extra_text),
            search_text=self._text_or_none(self._config.search_placeholder),
            search_expanded=self._config.search_expanded,
        )

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__} (",
            f"\tcurrent: UUID({self._current_album.uuid if self._current_album else 'None'})",
            ")"
        ))
