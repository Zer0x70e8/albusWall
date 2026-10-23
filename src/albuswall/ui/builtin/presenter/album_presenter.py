#
"""专辑网格 + 标题栏 presenter。

一块状态、两处呈现：``_current_album`` 是唯一事实，
网格高亮与 ``TitleBar`` 的 ``TitleBarVO`` 都从它派生。

封面是派生数据（虚拟相册取 scope 首项、物理相册取 ``cover_asset_id``
或最新资产），所以资产集合一变就要重新解析 —— 见 :meth:`reload_covers`。
解码 + 缩放放在线程池，路径解析留在主线程，回填按 uuid 对齐。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional
from uuid import UUID

from PySide6.QtCore import (
    Q_ARG,
    QMetaObject,
    QModelIndex,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QPixmap

import albuswall
from albuswall.dto.album import Album
from albuswall.log import Logger, getLogger
from albuswall.ui.vo.album import TitleBarVO

from ..config import (
    AlbumConfig,
    WindowConfig,
    album_config,
    resolve_icon_file,
    window_config,
)
from ..delegate.album_cover_delegate import AlbumCOverDelegate
from ..model.album_model import AlbumModel, AlbumRole
from ..widgets.square_grid import SquareGridView

if TYPE_CHECKING:
    from pathlib import Path

    from albuswall.services.view import ViewService

    from ..window.titlebar import TitleBar

__all__ = ["AlbumPresenter"]

_IconKind = Literal["album_icon", "extra_icon", "search_icon"]

_DEFAULT_TITLE = albuswall.__title__

_logger: Logger = getLogger(__name__)  # type: ignore

#: 后台解码时预缩放的短边像素。``KeepAspectRatioByExpanding`` 保证
#: 短边恰好等于它，paint 阶段就永远不需要放大。
_PREVIEW_EDGE = 512


def _read_cover_preview(path: str) -> QImage:
    """在线程池里把封面原图解码并预缩放一次。

    只碰文件与 ``QImage``，不碰任何 UI 对象 —— 结果通过
    ``invokeMethod`` 排队回主线程。
    """
    image = QImage(path)
    if image.isNull():
        return image
    if image.width() <= _PREVIEW_EDGE and image.height() <= _PREVIEW_EDGE:
        return image
    return image.scaled(
        _PREVIEW_EDGE,
        _PREVIEW_EDGE,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )


class _CoverJob(QRunnable):
    """把一个可调用对象丢进线程池；异常只记日志，不传播。"""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    # noinspection broad-exception
    def run(self) -> None:
        try:
            self._fn()
        except Exception:
            _logger.exception("Cover-loading task raised an exception")


class AlbumPresenter(QObject):
    """专辑面板 presenter。

    同时管两件事，状态只有一份（``_current_album``）：

    - 网格列表：从 ``ViewService`` 拉专辑 → 喂给 ``SquareGridView``；
    - 标题栏：当前专辑 + 偏好配置 → 打包成 ``TitleBarVO`` 交给 ``TitleBar``。

    两侧通过同一个 ``_current_album`` 保持一致：

    - 列表点击 → ``set_album`` → 刷 TitleBar + 高亮网格
    - 外部 ``set_album`` → 刷 TitleBar + 高亮网格

    接口契约（与 ViewService 收敛对齐）：视图服务只以 uuid 为键，
    本 presenter 不接触 ``Album.id`` / asset id。

    依赖 ``AlbumModel`` 至少提供以下接口::

        append(uuid: str, pixmap: QPixmap | None = None) -> int
        clear() -> None
        rowCount() -> int
        uuid_at(row: int) -> str
        pixmap_at(row: int) -> QPixmap | None
        setPixmap(row: int, pixmap: QPixmap) -> None
    """

    album_changed = Signal(Album)

    def __init__(
            self,
            title_bar: "TitleBar",
            album_view: SquareGridView,
            view_service: "ViewService",
            parent=None,
    ):
        super().__init__(parent)
        self._title_bar = title_bar
        self._view = album_view
        self._view_service = view_service
        # 配置一律是 typed dict 快照（默认值来自静态配置声明）
        self._config: AlbumConfig = album_config()
        self._window_config: WindowConfig = window_config()
        self._current_album: Album | None = None
        self._pool = QThreadPool.globalInstance()

        # 封面状态（只在主线程读写）：
        #   _cover_paths    —— uuid → 当前"想要的"封面路径（None = 无封面），
        #                      用于「路径没变就不重复解码原图」；
        #   _cover_inflight —— uuid → 正在后台解码的路径，避免同一封面重复入队。
        self._cover_paths: dict[str, Optional[str]] = {}
        self._cover_inflight: dict[str, str] = {}

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

    def _reload_config(self) -> None:
        """重新取一份 typed dict 快照（配置运行期可改，刷新时重读一次）。"""
        self._config = album_config()
        self._window_config = window_config()

    def setup(self, _=None) -> None:
        """首刷：列表（含封面）、默认专辑、标题栏，最后广播一次当前专辑。"""
        self._reload_config()
        self._populate_list()
        self._load_default_album()
        self._apply_window_settings()
        self._title_bar.set_vo(self._build_vo(self._current_album))
        self._sync_selection(self._current_album)
        self.album_changed.emit(self._current_album)
        _logger.trace(
            "view_service=%r module=%s",
            self._view_service, type(self._view_service).__module__)

    def teardown(self):
        # model / delegate / view 都由 Qt 父子关系或外部持有，这里没有
        # 需要主动释放的资源；线程池是全局的，不能也不能在这里关。
        return None

    def refresh(self) -> None:
        """重绘 TitleBar + 重新同步网格选中 + 重新解析封面。"""
        self._reload_config()
        self._apply_window_settings()
        self._title_bar.set_vo(self._build_vo(self._current_album))
        self._sync_selection(self._current_album)
        self.reload_covers()

    def retranslate(self) -> None:
        """语言切换后重刷标题栏（按钮兜底文案 / 搜索栏提示都来自视图）。"""
        self.refresh()

    def set_album(self, album: Album) -> None:
        """切换当前专辑：状态、TitleBar、网格高亮一起变。"""
        self._current_album = album
        self._title_bar.set_vo(self._build_vo(album))
        self._sync_selection(album)
        self.album_changed.emit(album)

    # ---------------- 网格数据 ----------------

    # noinspection broad-exception
    def _populate_list(self) -> None:
        """拉活动专辑列表，先塞占位，再异步回填封面。"""
        self._model.clear()
        self._cover_paths.clear()
        self._cover_inflight.clear()
        try:
            uuids = self._view_service.list_album_uuids()
        except Exception:
            _logger.exception("Failed to fetch the active album list")
            return

        _logger.trace("Active albums: %r", uuids)
        for u in uuids:
            self._model.append(str(u))
        self.reload_covers()

    def reload_covers(self) -> None:
        """重新解析并回填全部相册封面（资产集合变化后必须调用）。

        为什么需要它：封面是**派生数据**（虚拟相册取 scope 首项、物理相册取
        ``cover_asset_id`` 或最新资产），资产一导入 / 软删 / 恢复，封面就可能
        从「无」变成「有」或者换一张。而封面只在 ``_populate_list``（启动）
        时加载过一次，之后没有任何刷新入口 —— 启动时库里还没资产，封面就
        永远是空白（缩略图网格却有图），这正是「导入完了相册封面还是空的」
        的原因。

        契约：
            · 路径没变且已有 pixmap → 跳过（不重复解码原图）；
            · 路径变成 None        → 清空 pixmap，不显示已失效的旧封面；
            · 解析失败（DB/异常）  → 保留现状，宁可旧图也不闪成空白；
            · 幂等、开销小（每张封面一次轻量 DB 查询），可被高频调用。
        """
        for row in range(self._model.rowCount()):
            album_uuid = self._model.uuid_at(row)
            if not album_uuid:
                continue

            ok, path = self._resolve_cover_path(album_uuid)
            if not ok:
                continue  # 解析失败：保留当前封面

            if path == self._cover_paths.get(album_uuid) and (
                    path is None or self._model.pixmap_at(row) is not None
            ):
                continue  # 目标没变，且已经画出来了

            self._cover_paths[album_uuid] = path
            if path is None:
                self._cover_inflight.pop(album_uuid, None)
                self._model.setPixmap(row, QPixmap())
                continue

            if self._cover_inflight.get(album_uuid) == path:
                continue  # 同一目标已在后台解码
            self._cover_inflight[album_uuid] = path
            self._load_cover_async(album_uuid, path)

    def _resolve_cover_path(
            self, album_uuid: UUID | str,
    ) -> tuple[bool, Optional[str]]:
        """解析封面磁盘路径，返回 ``(ok, path)``。

        ``ok=False`` 表示解析本身失败（DB 异常等），调用方应保留现状；
        ``ok=True, path=None`` 表示"这个相册现在确实没有封面"。
        """
        try:
            raw = self._view_service.get_cover_full_path(album_uuid)
        except Exception:
            _logger.exception(
                "Failed to resolve the album cover album=%s", album_uuid
            )
            return False, None
        path = str(raw) if raw else None
        _logger.trace("cover path for album=%s -> %r", album_uuid, path)
        return True, path

    def _cover_path_or_none(self, album: Album | None) -> Optional[str]:
        """标题栏用的封面路径：解析失败按「无封面」处理，不影响渲染。"""
        if album is None:
            return None
        return self._resolve_cover_path(album.uuid)[1]

    def _load_cover_async(self, album_uuid: UUID | str, path: str) -> None:
        """后台把封面原图读成 QImage（顺带缩放一次），回主线程按 uuid 回填。

        路径解析在主线程完成（一次轻量 DB 查询，见 ``_resolve_cover_path``），
        解码 + 缩放留在后台线程；这样"路径没变"时完全不碰原图。
        """
        uuid_str, target = str(album_uuid), path

        def job() -> None:
            QMetaObject.invokeMethod(
                self, "_on_cover_ready_image",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, uuid_str),
                Q_ARG(str, target),
                Q_ARG(QImage, _read_cover_preview(target)),
            )

        self._pool.start(_CoverJob(job))

    @Slot(str, str, QImage)
    def _on_cover_ready_image(
            self, uuid_str: str, path: str, image: QImage,
    ) -> None:
        if self._cover_inflight.get(uuid_str) == path:
            self._cover_inflight.pop(uuid_str, None)

        # 解码期间数据又变了（封面换人 / 被删）：丢弃这次结果并立刻重新解析，
        # 否则这个 uuid 会一直停在旧画面上。
        if self._cover_paths.get(uuid_str) != path:
            self.reload_covers()
            return

        row = self._row_for_uuid(uuid_str)
        if row < 0:
            return  # 该专辑已不在列表里
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
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
        album = self._view_service.get_album(uuid_val)
        if album is None:
            _logger.debug("Clicked album %s resolved to nothing", uuid_val)
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
            2. 缺失时取 ``ViewService.list_album_uuids()`` 的第一个；
            3. 都没有则保持 ``None``。
        """
        album_uuid: UUID | str | None = self._config["default_album_uuid"]

        if not album_uuid:
            active = self._view_service.list_album_uuids()
            _logger.trace("presenter active = %r", active)
            album_uuid = active[0] if active else None

        if album_uuid is None:
            self._current_album = None
            _logger.trace(
                "No default album resolved; the title bar renders an empty album"
            )
            return

        self._current_album = self._view_service.get_album(album_uuid)
        if self._current_album is None:
            _logger.debug("Default album %s resolved to nothing", album_uuid)

    # ---------------- TitleBar VO ----------------

    def _window_title(self) -> str:
        return self._config["window_title"] or _DEFAULT_TITLE

    def _apply_window_settings(self) -> None:
        self._title_bar.window().setWindowTitle(self._window_title())

    @staticmethod
    def _text_or_none(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        s = str(value)
        return s if s != "" else None

    def _icon_path(self, kind: "_IconKind") -> "Optional[Path]":
        return resolve_icon_file(
            self._config, self._window_config, self._config[kind])

    def _build_vo(self, album: Album | None) -> TitleBarVO:
        return TitleBarVO(
            window_title=self._window_title(),
            cover=self._cover_path_or_none(album),
            title=self._text_or_none(album.title) if album is not None else None,
            description=(
                self._text_or_none(album.description) if album is not None else None
            ),
            album_icon=self._icon_path("album_icon"),
            extra_icon=self._icon_path("extra_icon"),
            search_icon=self._icon_path("search_icon"),
            album_text=self._text_or_none(self._config["album_text"]),
            extra_text=self._text_or_none(self._config["extra_text"]),
            search_text=self._text_or_none(self._config["search_placeholder"]),
            search_expanded=self._config["search_expanded"],
        )

    def __str__(self) -> str:
        album_uuid = self._current_album.uuid if self._current_album else "None"
        return "\n".join((
            f"{type(self).__name__} (",
            f"\tcurrent: UUID({album_uuid})",
            ")",
        ))
