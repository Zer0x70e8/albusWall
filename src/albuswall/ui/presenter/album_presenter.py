#
"""相册 presenter：相册列表 + 当前相册 + 标题栏文案。

一块状态、多处呈现
==================

``_current`` 是唯一事实来源：

* 相册页顶部网格（``Album.album_view``）与底部回收站（``Album.trash_view``）
  的高亮；
* 标题栏的主标题 / 副标题 / 封面图标（``TitleBar.set_*``）。

所以「切换相册」只有一条路径：:meth:`set_album`。它广播
``album_changed``，由主 presenter 把新相册转给内容网格 / 详情页。

相册页的两个列表按 scope 分流：**回收站（虚拟 Trash 相册）放底部**
（``trash_view``），其余相册（含虚拟 All）放顶部网格（``album_view``）。
两份模型都用 :class:`~albuswall.ui.model.AlbumModel`，共用同一份封面缓存。

封面是派生数据（虚拟相册取 scope 首项、物理相册取 ``cover_asset_id``
或最新资产），资产集合一变就要重新解析 —— 见 :meth:`reload_covers`。
解码走 :class:`~albuswall.ui.widgets.widget.image_loader.ImageLoader`（后台线程），
路径解析留在主线程，回填按 uuid 对齐。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from PySide6.QtCore import QModelIndex, QItemSelectionModel, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QInputDialog

from albuswall.dto.album.command import AlbumCreate
from albuswall.ui.model import AlbumModel, AlbumRole
from albuswall.ui.window.settings import window_state

from ..widgets import get_loader
from .base import Presenter, register_presenter
from .thumbnails import square_pixmap

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["AlbumPresenter"]

logger = logging.getLogger(__package__)

#: 封面缩略图的解码边长（短边），paint 时不再放大。
COVER_EDGE = 512

#: 回收站相册的行首图标（暂时用 emoji，将来换资源图只改这里）。
TRASH_ALBUM_ICON = "🗑️"


class AlbumPresenter(Presenter):
    """相册列表 / 当前相册的 presenter。"""

    name = "album"

    #: 当前相册发生变化（载荷是 ``Album | None``）。
    album_changed = Signal(object)
    #: 相册列表本身发生变化（新增 / 删除 / 重排）。
    album_list_changed = Signal()
    #: 面向用户的操作结果 / 错误（窗口 toast）。
    error = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._page = window.album
        self._title_bar = window.title_bar
        self._view_service = self.service("view_service")
        self._album_service = self.service("album_service")

        self._model = AlbumModel(parent=self)
        self._trash_model = AlbumModel(parent=self)
        self._current: Any | None = None

        # 封面状态（只在主线程读写）：
        #   _cover_paths    —— uuid → 当前"想要的"封面路径（None = 无封面）
        #   _cover_inflight —— uuid → 正在后台解码的路径（避免重复入队）
        self._cover_paths: dict[str, str | None] = {}
        self._cover_inflight: dict[str, str] = {}
        self._loader = get_loader()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        # 顶部：相册网格；底部：回收站（页面负责缺省委托装配）
        self._page.set_album_model(self._model)
        self._page.set_trash_model(self._trash_model)
        for view in (self._page.album_view, self._page.trash_view):
            view.clicked.connect(self._on_album_clicked)
            view.activated.connect(self._on_album_clicked)
        self._page.edit_button.clicked.connect(self.rename_current)
        self._page.add_button.clicked.connect(self.create_album)

        if self._loader is not None:
            self._loader.thumb_loaded.connect(self._on_cover_loaded)

        self.refresh()
        # 恢复上次退出时的相册（``refresh`` 已经选好默认相册，这里再覆盖）。
        self.restore_state()

    def on_stop(self) -> None:
        if self._loader is not None:
            try:
                self._loader.thumb_loaded.disconnect(self._on_cover_loaded)
            except (RuntimeError, TypeError):
                pass

    def retranslate(self) -> None:
        """语言切换：相册列表的封面 / 标题都要重刷（默认标题是译文）。"""
        self.refresh()

    # ------------------------------------------------------------------ #
    # 当前相册
    # ------------------------------------------------------------------ #
    @property
    def model(self) -> AlbumModel:
        """顶部相册网格的模型（全部相册，含虚拟 All）。"""
        return self._model

    @property
    def trash_model(self) -> AlbumModel:
        """底部回收站的模型（只含虚拟 Trash 相册）。"""
        return self._trash_model

    @property
    def current(self) -> Any | None:
        return self._current

    @property
    def current_uuid(self) -> UUID | None:
        return None if self._current is None else self._current.uuid

    def set_album(self, album: Any | None) -> None:
        """切换当前相册：状态、标题栏、网格高亮一起变（单向出口）。"""
        if album is self._current:
            return
        if (
                album is not None
                and self._current is not None
                and str(album.uuid) == str(self._current.uuid)
        ):
            # 同一相册的新 DTO（标题等可能变了）：刷新展示但不广播切换
            self._current = album
            self._apply_chrome(album)
            return
        self._current = album
        self._apply_chrome(album)
        self._sync_selection(album)
        self.album_changed.emit(album)

    def set_album_uuid(self, album_uuid: Any) -> None:
        """按 uuid 切换相册（找不到时记日志并忽略）。"""
        album = self._get_album(album_uuid)
        if album is None:
            return
        self.set_album(album)

    def _get_album(self, album_uuid: Any) -> Any | None:
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_album(album_uuid)
        except Exception:  # noqa: BLE001 - 读相册失败按"不存在"处理
            logger.exception("album presenter: get_album failed: %s", album_uuid)
            return None

    # ------------------------------------------------------------------ #
    # 界面状态（``[window] album_uuid``）
    # ------------------------------------------------------------------ #
    def restore_state(self) -> None:
        """按 ``[window] album_uuid`` 恢复上次的相册（找不到就保持默认相册）。

        「找不到」包括：从没存过、uuid 非法、相册已被删除 ——
        :meth:`set_album_uuid` 对不存在相册只记日志并忽略，正好是想要的语义。
        """
        saved = str(window_state().album_uuid or "").strip()
        if not saved:
            return
        if self.current_uuid is not None and str(self.current_uuid) == saved:
            return
        self.set_album_uuid(saved)
        logger.debug("album presenter: album %s restored from config", saved)

    def ui_state(self) -> dict[str, object]:
        """要跨会话记住的相册（空串 = 没有当前相册）。"""
        current = self.current_uuid
        return {"window.album_uuid": "" if current is None else str(current)}

    # ------------------------------------------------------------------ #
    # 列表
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """全量刷新：列表 → 当前相册 → 封面 → 标题栏 / 高亮。"""
        self._populate_list()
        current_uuid = self.current_uuid
        album = self._get_album(current_uuid) if current_uuid is not None else None
        if album is None:
            album = self._default_album()
        self._refresh_current(album)

    def _refresh_current(self, album: Any | None) -> None:
        """只更新当前相册（不重建列表）。"""
        if album is None:
            self._current = None
            self._apply_chrome(None)
            self.album_changed.emit(None)
            return
        self._current = album
        self._apply_chrome(album)
        self._sync_selection(album)
        self.album_changed.emit(album)

    def _populate_list(self) -> None:
        """拉相册 uuid 列表 → 按 scope 分流成「相册」与「回收站」两组。

        回收站是虚拟 Trash 相册（``scope == deleted``），固定放底部；
        其余（含虚拟 All）放顶部网格。封面在两类之间共用一份缓存。
        """
        if self._view_service is None:
            logger.warning("album presenter: view_service unavailable")
            return
        try:
            uuids = self._view_service.list_album_uuids()
        except Exception:  # noqa: BLE001
            logger.exception("album presenter: list_album_uuids failed")
            return

        albums: list[Any] = []
        trash: list[Any] = []
        for album_uuid in uuids:
            album = self._get_album(album_uuid)
            if album is None:
                continue
            if self._scope_of(album_uuid) == "deleted":
                trash.append(album)
            else:
                albums.append(album)

        self._cover_paths.clear()
        self._cover_inflight.clear()
        self._model.set_albums(albums)
        # 回收站是单行列表（左图标 / 弹簧 / 右名称）：图标由这里统一下发，
        # 名称仍取相册 DTO 的 title。
        self._trash_model.set_albums(trash, icon=TRASH_ALBUM_ICON)
        self.reload_covers()
        logger.debug(
            "album presenter: %d albums + %d trash loaded",
            len(albums), len(trash))

    def _scope_of(self, album_uuid: Any) -> str | None:
        """相册 scope（``None`` = 物理相册，``"deleted"`` = 回收站）。"""
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_scope(album_uuid)
        except Exception:  # noqa: BLE001
            logger.exception("album presenter: get_scope failed")
            return None

    def _default_album(self) -> Any | None:
        """当前相册缺失时的兜底：顶部相册网格的第一个。"""
        if self._model.rowCount() <= 0:
            return None
        return self._get_album(self._model.uuid_at(0))

    # ------------------------------------------------------------------ #
    # 封面
    # ------------------------------------------------------------------ #
    def reload_covers(self) -> None:
        """重新解析并回填全部封面（资产集合变化后调用，幂等、可高频调）。"""
        if self._view_service is None:
            return
        for model in (self._model, self._trash_model):
            for row in range(model.rowCount()):
                album_uuid = model.uuid_at(row)
                if not album_uuid:
                    continue
                path = self._resolve_cover_path(album_uuid)
                current = self._cover_paths.get(album_uuid)
                if path == current and (
                        path is None or model.pixmap_at(row) is not None
                ):
                    continue
                self._cover_paths[album_uuid] = path
                if path is None:
                    self._cover_inflight.pop(album_uuid, None)
                    model.set_pixmap(row, None)
                    continue
                if self._cover_inflight.get(album_uuid) == path:
                    continue
                self._cover_inflight[album_uuid] = path
                if self._loader is not None:
                    self._loader.load_thumb(
                        path, COVER_EDGE, f"cover:{album_uuid}")

    def _resolve_cover_path(self, album_uuid: Any) -> str | None:
        try:
            raw = self._view_service.get_cover_full_path(album_uuid)
        except Exception:  # noqa: BLE001 - 封面解析失败按"无封面"处理
            logger.exception(
                "album presenter: resolve cover failed: %s", album_uuid)
            return None
        return str(raw) if raw else None

    def _on_cover_loaded(self, token: str, pixmap: QPixmap) -> None:
        if not token.startswith("cover:"):
            return
        album_uuid = token[len("cover:"):]
        self._cover_inflight.pop(album_uuid, None)
        if self._cover_paths.get(album_uuid) is None:
            return
        square = square_pixmap(pixmap, edge=COVER_EDGE)
        for model in (self._model, self._trash_model):
            row = model.row_of(album_uuid)
            if row >= 0:
                model.set_pixmap(row, square)

    # ------------------------------------------------------------------ #
    # 标题栏
    # ------------------------------------------------------------------ #
    def _apply_chrome(self, album: Any | None) -> None:
        """把当前相册写进标题栏的工具栏（主标题 / 副标题 / 封面）。

        窗口标题（``Window.windowTitle``）是**应用级**信息，由 boot 设置并
        可能被配置覆盖；相册只是业务文案，只进 ``TitleBar.title_label`` /
        ``sub_title``，不参与窗口标题 —— 否则操作栏会把相册名当成窗口名。
        """
        title = "" if album is None else str(getattr(album, "title", "") or "")
        description = (
            "" if album is None else str(getattr(album, "description", "") or "")
        )
        self._title_bar.set_title(title)
        self._title_bar.set_description(description)

        cover = None if album is None else self._cover_paths.get(str(album.uuid))
        self._title_bar.set_icon(cover)

    def _sync_selection(self, album: Any | None) -> None:
        """让网格（或底部回收站）高亮 / 滚动到当前相册。"""
        for model, view in (
                (self._model, self._page.album_view),
                (self._trash_model, self._page.trash_view),
        ):
            if album is None:
                view.clearSelection()
                continue
            row = model.row_of(str(album.uuid))
            if row < 0:
                view.clearSelection()
                continue
            index = model.index(row, 0)
            view.setCurrentIndex(index)
            selection = view.selectionModel()
            if selection is not None:
                selection.select(
                    index,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
            view.scrollTo(index)

    # ------------------------------------------------------------------ #
    # 相册的增 / 改
    # ------------------------------------------------------------------ #
    def create_album(self) -> None:
        """新建相册（标题从输入框拿，创建成功后切到新相册）。"""
        if self._album_service is None:
            self.error.emit(self.tr("The album service is unavailable"))
            return
        title = self._ask_title(self.tr("New album"))
        if not title:
            return
        try:
            album = self._album_service.create(AlbumCreate(title=title))
        except Exception as exc:  # noqa: BLE001
            logger.exception("album presenter: create album failed")
            self.error.emit(str(exc))
            return
        self.refresh()
        self.set_album(album)
        self.album_list_changed.emit()

    def rename_current(self) -> None:
        """重命名当前相册。"""
        if self._album_service is None or self._current is None:
            return
        album = self._current
        title = self._ask_title(
            self.tr("Rename album"),
            initial=str(getattr(album, "title", "") or ""),
        )
        if not title or title == getattr(album, "title", ""):
            return
        try:
            self._album_service.rename(album.uuid, title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("album presenter: rename album failed")
            self.error.emit(str(exc))
            return
        self.refresh()

    def _ask_title(self, title: str, *, initial: str = "") -> str:
        """弹一个「相册标题」输入框；取消 / 空输入返回空串。"""
        dialog = QInputDialog(self._window)
        dialog.setWindowTitle(title)
        dialog.setLabelText(self.tr("Album title:"))
        dialog.setTextValue(initial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        return dialog.textValue().strip()

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_album_clicked(self, index: QModelIndex) -> None:
        album_uuid = index.data(int(AlbumRole.Uuid))
        if not album_uuid:
            return
        self.set_album_uuid(album_uuid)


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_album_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("album presenter skipped: no window bound")
        return None
    return AlbumPresenter(container, window=window, parent=main)
