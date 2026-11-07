#
"""内容网格 presenter：主内容层（``Window.content``）里的资产缩略图。

数据来源
========

* **uuid 列表** —— ``ViewService.get_asset_uuids(album)``（一次轻量查询，
  虚拟相册 / 物理相册由服务自己分派，presenter 不关心）；
* **单张信息** —— 只在格子进入可见范围时 ``ViewService.get_asset(uuid)`` 懒查
  （标题 / 收藏 / 回收站剩余天数）。这一步顺带触发服务层的
  ``assets_viewed`` 广播 —— 按需缩略图生成就是从这里被点着的；
* **缩略图文件** —— ``ThumbnailService.get_thumbnail_path`` 取路径，没有就让
  服务补生成；像素解码交给 :class:`~albuswall.ui.widgets.image_loader.ImageLoader`
  的线程池，回主线程按行号回填 ``AssetGridModel``。

绘制
====

内容层就是一个 ``QListView``（
:class:`~albuswall.ui.widgets.tile_view.TileView`），模型直接投给视图，
**委托仍由这里显式装上**（``QListView`` 默认委托只画图标 + 文字，收藏角标
就丢了）。这里装的是 :class:`~albuswall.ui.delegate.InfoCellDelegate` 的
``AUTO`` 模式：

* 被回收的格子（``ItemRole.DaysLeft`` 有值）→ 显示回收站剩余天数；
* 其余格子 → 显示收藏状态（已收藏画主题里的 ``favorite_icon``，没有图标
  时降级成 ``★``）。

``AUTO`` 的逐格判断在委托内部完成，presenter 不按相册类型切换模式；
主题切换时通过 ``MainPresenter.theme_changed`` 重新装收藏图标。

主内容层是 ``Window.content``（虚拟滚动网格）唯一的资产视图：相册页顶部
放的是相册网格、底部是回收站，都不展示资产。

标签过滤
========

:meth:`set_tag_filter` 只影响「显示哪些行」：它把相册 uuid 序列与标签 uuid
序列求交后重建模型。过滤条件在切换相册时清除（同一标签在新相册里可能
一张图都没有）。

右键菜单
========

右键某一格给出两个动作（:meth:`build_asset_menu`）：

* **移动到指定相册** —— 二级菜单列出所有可写相册，末尾「新建相册并移入…」；
* **从本相册移除** —— 只摘成员关系，不删资产。

按当前相册的类型给出不同语义：

* **物理相册**：「移动到指定相册」（``move_between_albums``，单事务、真原子）
  + 「从本相册移除」（``remove_from_album``）；
* **虚拟「全部」视图**：它只是聚合，没有成员关系 —— 菜单变成「添加到指定相册」
  （``add_to_album``，不移动），移除项禁用；
* **虚拟「回收站」**：两个选项都禁用并给出提示。

写成功后重拉当前相册列表并广播 ``assets_changed``，让相册封面等跟随刷新。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCursor, QIcon, QPixmap
from PySide6.QtWidgets import QInputDialog, QMenu

from albuswall.common.enums import ThumbSpec
from albuswall.dto.album.command import AlbumCreate
from albuswall.service.view.service import SCOPE_DELETED
from albuswall.ui.delegate import InfoCellDelegate, InfoMode
from albuswall.ui.model import AssetEntry, AssetGridModel
from albuswall.ui.window.settings import window_state

from ..widgets.image_loader import get_loader
from ..widgets.tile_view import DEFAULT_COLUMN_COUNT
from .base import Presenter, register_presenter
from .thumbnails import square_pixmap

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["ContentPresenter"]

logger = logging.getLogger(__package__)

#: 网格用的缩略图规格（格子边长在 128~512 之间时最合适）。
GRID_SPEC = ThumbSpec.MEDIUM

#: 解码边长（与 ``GRID_SPEC`` 的像素档位一致；``ImageLoader`` 的缩略图缓存键）。
GRID_EDGE = 512

#: 回收站保留天数（与 ``service/asset/trash`` 的 ``TRASH_GRACE`` 同口径）。
TRASH_RETENTION_DAYS = 30

#: 加载 token 前缀：区分「网格缩略图」与其它 presenter 的加载请求。
TOKEN_PREFIX = "grid:"

#: 收藏角标的图标逻辑名 —— ``[ui]`` / ``[files]`` 的同名键，文件名与目录都由
#: 配置的 ``${...}`` 组合（见 :mod:`albuswall.ui.presenter.theme`）。
#:
#: 这张「心形」是**缩略图角标**，不是按钮图标 —— 详情页的收藏按钮和其它动作
#: 按钮一样用文字（见 :mod:`albuswall.ui.presenter.window_presenter`）。
FAVORITE_ICON_NAME = "favorite_icon"


def remaining_days(deleted_at: Any) -> int | None:
    """``deleted_at`` → 距回收站保留期到期的天数（None = 不在回收站）。"""
    if not deleted_at:
        return None
    try:
        moment = datetime.fromisoformat(str(deleted_at))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - moment).total_seconds() / 86400.0
    return max(0, int(TRASH_RETENTION_DAYS - elapsed))


class ContentPresenter(Presenter):
    """主内容层（相册资产网格）的 presenter。"""

    name = "content"

    #: 用户点击 / 回车激活了某一格（载荷 asset_uuid）。
    asset_activated = Signal(str)
    #: 用户按空格「快速预览」了某一格（载荷 asset_uuid）：与回车（详情）分开。
    asset_previewed = Signal(str)
    #: 键盘 / 鼠标光标切换（载荷 asset_uuid；空串 = 没有选中项）。
    selection_changed = Signal(str)
    #: 数据面变化（刷新完成；主 presenter 据此联动其它 presenter）。
    assets_changed = Signal()
    #: 面向用户的成功提示（窗口 toast）。
    notice = Signal(str)
    #: 面向用户的错误提示（窗口 toast）；主 presenter 会自动接线。
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
        self._view_service = self.service("view_service")
        self._thumb_service = self.service("thumbnail_service")
        self._tag_service = self.service("tag_service")
        self._asset_ops = self.service("asset_ops_service")
        self._album_service = self.service("album_service")

        self._model = AssetGridModel(parent=self)
        #: 内容网格的绘制委托：``AUTO`` = 逐格判断「回收站剩余天数 / 收藏状态」。
        #: ``QListView`` 的默认委托只画图标和文字，角标必须由这里装。
        self._delegate = InfoCellDelegate(mode=InfoMode.AUTO)
        #: 主题信号（用于重新装收藏角标图标）；没有主 presenter 时为 ``None``。
        self._theme_signal: Any | None = None
        self._loader = get_loader()

        self._album_uuid: Any | None = None
        self._include_deleted = False
        self._tag_filter: str | None = None

        # 懒加载状态（只在主线程读写）
        self._uids: list[str] = []
        self._info_loaded: set[str] = set()
        self._thumb_requested: set[str] = set()
        self._visible: tuple[int, int] = (0, -1)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        grid = self._window.content
        # 委托先装：``QListView`` 会在 setModel 之后立刻布局并绘制。
        grid.setItemDelegate(self._delegate)
        grid.setModel(self._model)
        grid.visible_range_changed.connect(self._on_visible_range)
        grid.unit_activated.connect(self._on_unit_activated)
        grid.unit_previewed.connect(self._on_unit_previewed)
        grid.unit_clicked.connect(self._on_unit_activated)
        grid.unit_right_clicked.connect(self._on_unit_right_clicked)
        grid.cursor_changed.connect(self._on_cursor_changed)

        # 恢复上次退出时的网格缩放（列数）：必须在 ``setModel`` 之后 ——
        # 换列数会强制重新布局并重算格子尺寸。
        self.restore_state()

        self._wire_theme()

        # 首次装载发生在 ``setModel`` 之前（相册已经在父节点启动时选好），
        # 那时视图还没有数据、光标是 -1；``setModel`` 之后视图才把光标落到
        # 第一格。这里补播一次，标签栏开机就指向光标资产。
        self._emit_current_selection()

        if self._thumb_service is not None:
            self._thumb_service.thumbnail_ready.connect(
                self._on_thumbnail_ready)
        if self._loader is not None:
            self._loader.thumb_loaded.connect(self._on_thumb_loaded)

    def on_stop(self) -> None:
        signal = self._theme_signal
        self._theme_signal = None
        if signal is not None:
            try:
                signal.disconnect(self.apply_theme)
            except (RuntimeError, TypeError):
                pass
        if self._loader is not None:
            try:
                self._loader.thumb_loaded.disconnect(self._on_thumb_loaded)
            except (RuntimeError, TypeError):
                pass
        if self._thumb_service is not None:
            try:
                self._thumb_service.thumbnail_ready.disconnect(
                    self._on_thumbnail_ready)
            except (RuntimeError, TypeError):
                pass

    def retranslate(self) -> None:
        """语言切换：网格里没有文案（标题来自文件名），只需重绘。"""
        self._window.content.update()

    # ------------------------------------------------------------------ #
    # 界面状态（``[window] tile_columns``）
    # ------------------------------------------------------------------ #
    def restore_state(self) -> None:
        """按 ``[window] tile_columns`` 恢复网格列数（``0`` = 由视图换算）。

        越界值（小于最小列数 / 大于最大列数）由
        :meth:`~albuswall.ui.widgets.tile_view.TileView.set_column_count`
        自己 clamp，这里不做二次判断。
        """
        columns = int(window_state().tile_columns or 0)
        if columns <= 0:
            return
        grid = self._window.content
        setter = getattr(grid, "set_column_count", None)
        if not callable(setter):
            return
        try:
            setter(columns)
        except (RuntimeError, TypeError, ValueError):
            logger.exception(
                "content presenter: cannot restore tile_columns=%s", columns)
            return
        logger.debug("content presenter: tile_columns=%s restored", columns)

    def ui_state(self) -> dict[str, object]:
        """要跨会话记住的网格缩放（列数）。

        **列数等于视图默认值时不记**（写 ``0`` = 「没指定」）：用户没缩放过
        网格却把 ``tile_columns = 5`` 写进配置文件，等于把「默认值」冻结成
        「用户的选择」—— 将来改默认列数，老用户不会跟着变。
        """
        columns = getattr(self._window.content, "column_count", 0)
        try:
            value = int(columns)
        except (TypeError, ValueError):
            return {"window.tile_columns": 0}
        if value <= 0 or value == DEFAULT_COLUMN_COUNT:
            return {"window.tile_columns": 0}
        return {"window.tile_columns": value}

    # ------------------------------------------------------------------ #
    # 主题：收藏角标图标
    # ------------------------------------------------------------------ #
    def _wire_theme(self) -> None:
        """跟主 presenter 的主题走：首次装图标 + 主题切换时重装。"""
        parent = self.parent
        signal = getattr(parent, "theme_changed", None)
        if signal is not None:
            try:
                signal.connect(self.apply_theme)
                self._theme_signal = signal
            except (RuntimeError, TypeError):
                logger.warning(
                    "content presenter: cannot hook theme_changed")
        self.apply_theme(getattr(parent, "theme", None))

    def apply_theme(self, theme: Any | None) -> None:
        """把主题的 ``favorite_icon`` 装成格子的收藏角标。

        ``theme`` 为 ``None``（或主题里没有这张图）时委托回退成 ``★`` 文本，
        不会出现「没有图标就什么都不画」的空状态。
        """
        path = None
        if theme is not None:
            icon_lookup = getattr(theme, "icon", None)
            if callable(icon_lookup):
                try:
                    path = icon_lookup(FAVORITE_ICON_NAME)
                except Exception:  # noqa: BLE001 - 主题坏了不该拖垮网格
                    logger.exception(
                        "content presenter: theme icon lookup failed")
                    path = None

        icon = None
        if path is not None:
            icon = QIcon(str(path))
            if icon.isNull():
                icon = None
        self._delegate.set_icon(icon, checked=True)
        self._window.content.update()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def model(self) -> AssetGridModel:
        return self._model

    @property
    def delegate(self) -> InfoCellDelegate:
        """内容网格的委托（``AUTO`` 模式：逐格显示天数 / 收藏）。"""
        return self._delegate

    @property
    def album_uuid(self) -> Any | None:
        return self._album_uuid

    @property
    def tag_filter(self) -> str | None:
        return self._tag_filter

    def set_album(self, album: Any | None) -> None:
        """切换到某个相册的资产集合（会清掉标签过滤）。"""
        self._album_uuid = None if album is None else album.uuid
        self._include_deleted = (
                album is not None
                and self._scope_of(album.uuid) == SCOPE_DELETED
        )
        self._tag_filter = None
        self.reload()

    def reload(self) -> None:
        """按当前相册 + 过滤条件重新拉 uuid 列表并重建模型。"""
        if self._view_service is None or self._album_uuid is None:
            self._uids = []
            self._model.clear()
            self.assets_changed.emit()
            return
        try:
            uuids = [
                str(u)
                for u in self._view_service.get_asset_uuids(self._album_uuid)
            ]
        except Exception:  # noqa: BLE001 - 读列表失败按空相册处理
            logger.exception(
                "content presenter: get_asset_uuids failed: %s",
                self._album_uuid)
            uuids = []

        self.set_assets(self._filter_by_tag(uuids))

    def set_assets(self, uuids: list[str]) -> None:
        """整体替换资产集合（模型 reset → 两个网格自动同步行数）。"""
        self._uids = [str(u) for u in uuids]
        self._info_loaded.clear()
        self._thumb_requested.clear()
        self._model.set_entries(AssetEntry(uuid=u) for u in self._uids)
        logger.debug(
            "content presenter: %d assets loaded (album=%s, filter=%s)",
            len(self._uids), self._album_uuid, self._tag_filter)
        self._load_visible()
        # 换源时光标由视图自己挪（``emit=False``），不会发 ``cursor_changed``；
        # 这里补播一次，标签栏这类「跟随当前资产」的消费者才拿得到新对象。
        self._emit_current_selection()
        self.assets_changed.emit()

    def refresh(self) -> None:
        """数据面可能变了（导入 / 软删 / 恢复后）：重拉列表并重载可见行。

        列表没变时不整体 reset（避免网格闪一下），只重查可见行的信息与
        缩略图；变了才走 ``set_assets`` 重建。
        """
        if self._view_service is None or self._album_uuid is None:
            self.reload()
            return
        try:
            fresh = [
                str(u)
                for u in self._view_service.get_asset_uuids(
                    self._album_uuid)
            ]
        except Exception:  # noqa: BLE001 - 读失败就保留现状
            logger.exception("content presenter: refresh failed")
            return
        filtered = self._filter_by_tag(fresh)
        if filtered != self._uids:
            self.set_assets(filtered)
            return
        self._info_loaded.clear()
        self._thumb_requested.clear()
        self._load_visible()

    def set_tag_filter(self, tag_uuid: str | None) -> None:
        """按标签过滤当前相册内的资产；``None`` 清除过滤。"""
        tag_uuid = str(tag_uuid).strip() if tag_uuid else None
        if tag_uuid == self._tag_filter:
            return
        self._tag_filter = tag_uuid or None
        self.reload()

    def current_asset_uuid(self) -> str | None:
        """键盘光标所在资产的 uuid（没有数据 / 没有光标时为 ``None``）。"""
        index = self._window.content.cursor_index()
        if 0 <= index < len(self._uids):
            return self._uids[index]
        return None

    def select_asset(self, asset_uuid: str) -> None:
        """把光标移到指定资产（翻页 / 外部跳转用）。"""
        try:
            row = self._uids.index(str(asset_uuid))
        except ValueError:
            return
        self._window.content.set_cursor_index(row)

    # ------------------------------------------------------------------ #
    # 快捷键入口（asset.*）：详情页没开时由网格光标所在资产承担
    # ------------------------------------------------------------------ #
    def toggle_favorite(self) -> bool:
        """``asset.favorite``：收藏 / 取消收藏网格光标所在资产。

        「当前是否已收藏」优先读模型里的懒查结果；模型还是「未知」
        （``None``）时按未收藏处理，点一下就是收藏。
        """
        asset_uuid = self.current_asset_uuid()
        if asset_uuid is None or self._asset_ops is None:
            return False
        row = self._model.row_of(asset_uuid)
        entry = self._model.entry_at(row) if row >= 0 else None
        current = bool(getattr(entry, "favorite", False))
        target = not current
        try:
            result = self._asset_ops.set_favorite(asset_uuid, target)
        except Exception as exc:  # noqa: BLE001 - 写库失败转成 toast
            logger.exception("content presenter: set_favorite failed")
            self.error.emit(str(exc))
            return False
        if getattr(result, "outcome", "") == "not_found":
            return False
        if row >= 0:
            self._model.set_favorite(row, target)
        self.assets_changed.emit()
        return True

    def trash_current(self) -> bool:
        """``asset.delete``：把网格光标所在资产移入回收站。"""
        asset_uuid = self.current_asset_uuid()
        if asset_uuid is None or self._asset_ops is None:
            return False
        try:
            result = self._asset_ops.trash([asset_uuid])
        except Exception as exc:  # noqa: BLE001 - 写库失败转成 toast
            logger.exception("content presenter: trash failed")
            self.error.emit(str(exc))
            return False
        changed = [str(u) for u in (getattr(result, "changed", ()) or ())]
        if not changed:
            return False
        # 列表去掉这一格（refresh 走「列表变了 → 重建模型」那条路），
        # 再广播一次数据面变化，让相册封面等跟着刷新。
        self.refresh()
        self.assets_changed.emit()
        return True

    def restore_current(self) -> bool:
        """``asset.restore``：把网格光标所在资产从回收站恢复。"""
        asset_uuid = self.current_asset_uuid()
        if asset_uuid is None or self._asset_ops is None:
            return False
        try:
            result = self._asset_ops.restore([asset_uuid])
        except Exception as exc:  # noqa: BLE001 - 写库失败转成 toast
            logger.exception("content presenter: restore failed")
            self.error.emit(str(exc))
            return False
        restored = [str(u) for u in (getattr(result, "restored", ()) or ())]
        if not restored:
            return False
        self.refresh()
        self.assets_changed.emit()
        return True

    def preview_current(self) -> bool:
        """``content.preview``：对光标所在资产发起快速预览（与空格同一条出口）。"""
        asset_uuid = self.current_asset_uuid()
        if asset_uuid is None:
            return False
        self.asset_previewed.emit(asset_uuid)
        return True

    # ------------------------------------------------------------------ #
    # 过滤
    # ------------------------------------------------------------------ #
    def _scope_of(self, album_uuid: Any) -> str | None:
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_scope(album_uuid)
        except Exception:  # noqa: BLE001
            logger.exception("content presenter: get_scope failed")
            return None

    def _filter_by_tag(self, uuids: list[str]) -> list[str]:
        """把相册 uuid 序列与标签 uuid 序列求交（保持相册顺序）。"""
        if not self._tag_filter or self._tag_service is None:
            return uuids
        try:
            tagged = {
                str(u)
                for u in self._tag_service.asset_uuids(
                    self._tag_filter,
                    scope=SCOPE_DELETED if self._include_deleted else "active",
                )
            }
        except Exception:  # noqa: BLE001 - 过滤失败不如"不过滤"好用
            logger.exception(
                "content presenter: tag filter failed: %s", self._tag_filter)
            return uuids
        return [u for u in uuids if u in tagged]

    # ------------------------------------------------------------------ #
    # 懒加载
    # ------------------------------------------------------------------ #
    def _load_visible(self) -> None:
        """按当前可见范围补齐信息与缩略图（还没量过就先补头一屏）。"""
        start, end = self._visible
        if end < start:
            start, end = 0, min(len(self._uids) - 1, 23)
        self._load_rows(range(start, end + 1))

    def _load_rows(self, rows: Any) -> None:
        for row in rows:
            if 0 <= row < len(self._uids):
                self._ensure_row(row)

    def _ensure_row(self, row: int) -> None:
        """补齐一行的信息与缩略图（幂等）。"""
        asset_uuid = self._uids[row]

        if asset_uuid not in self._info_loaded:
            self._load_info(row, asset_uuid)

        if self._model.pixmap_at(row) is not None:
            return
        if self._thumb_service is None:
            return

        # 按需生成：没有路径才提交（服务内部有去重 / 冷却）
        try:
            path = self._thumb_service.get_thumbnail_path(
                asset_uuid, GRID_SPEC)
        except Exception:  # noqa: BLE001
            logger.exception("content presenter: thumbnail path lookup failed")
            return
        if not path:
            if asset_uuid not in self._thumb_requested:
                self._thumb_requested.add(asset_uuid)
                try:
                    self._thumb_service.request(
                        asset_uuid, include_deleted=self._include_deleted)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "content presenter: thumbnail request failed")
            return

        if self._loader is not None:
            self._loader.load_thumb(
                path, GRID_EDGE, f"{TOKEN_PREFIX}{asset_uuid}")

    def _load_info(self, row: int, asset_uuid: str) -> None:
        """懒查单张资产的展示字段（标题 / 收藏 / 回收站天数）。"""
        if self._view_service is None:
            self._info_loaded.add(asset_uuid)
            return
        try:
            asset = self._view_service.get_asset(
                asset_uuid, include_deleted=self._include_deleted)
        except Exception:  # noqa: BLE001
            logger.exception(
                "content presenter: get_asset failed: %s", asset_uuid)
            self._info_loaded.add(asset_uuid)
            return
        self._info_loaded.add(asset_uuid)
        if asset is None:
            return

        title = str(
            getattr(asset, "original_name", "")
            or getattr(asset, "file_path", "")
            or "")
        days = remaining_days(getattr(asset, "deleted_at", None))
        self._model.patch_entry(
            row,
            title=title,
            favorite=bool(getattr(asset, "is_favorite", False)),
            days_left=days,
            deleted=bool(getattr(asset, "is_deleted", False)),
        )

    # ------------------------------------------------------------------ #
    # 右键菜单：移动到相册 / 从本相册移除
    # ------------------------------------------------------------------ #
    def build_asset_menu(self, asset_uuid: str) -> QMenu:
        """为某一格构建右键菜单（不 exec，方便测试直接触发动作）。"""
        menu = QMenu(self._window.content)
        menu.setObjectName("ContentContextMenu")

        mutable = self._album_is_mutable()
        aggregate = self._album_scope() == "active"
        move_allowed = mutable or aggregate

        if not move_allowed:
            hint = menu.addAction(
                self.tr("“Trash” is not an album, so assets cannot be moved or removed"))
            hint.setEnabled(False)
            menu.addSeparator()

        move_menu = menu.addMenu(
            self.tr("Move to album") if mutable
            else self.tr("Add to album"))
        move_menu.setObjectName("ContentContextMenuMoveMenu")
        self._populate_move_menu(move_menu, asset_uuid)
        move_menu.setEnabled(move_allowed)

        remove_action = menu.addAction(self.tr("Remove from this album"))
        remove_action.setEnabled(mutable)
        remove_action.triggered.connect(
            lambda _checked=False: self.remove_asset_from_album(asset_uuid))
        return menu

    def _populate_move_menu(self, menu: QMenu, asset_uuid: str) -> None:
        """填「移动到指定相册」二级菜单：可写相册 + 新建并移入。"""
        targets = self._movable_albums()
        if not targets:
            empty = menu.addAction(self.tr("(no other album yet)"))
            empty.setEnabled(False)
        for album in targets:
            action = menu.addAction(str(getattr(album, "title", "") or ""))
            action.triggered.connect(
                lambda _checked=False, uuid=album.uuid: self.move_asset_to_album(
                    asset_uuid, uuid))
        menu.addSeparator()
        create_action = menu.addAction(self.tr("New album and move into it…"))
        create_action.triggered.connect(
            lambda _checked=False: self.create_album_and_move(asset_uuid))

    def _movable_albums(self) -> list[Any]:
        """可选的目标相册：物理相册，且不是当前相册。"""
        if self._view_service is None:
            return []
        try:
            uuids = self._view_service.list_album_uuids()
        except Exception:  # noqa: BLE001 - 读列表失败按「没有目标」处理
            logger.exception("content presenter: list_album_uuids failed")
            return []
        current = None if self._album_uuid is None else str(self._album_uuid)
        targets: list[Any] = []
        for album_uuid in uuids:
            if str(album_uuid) == current:
                continue
            if self._scope_of(album_uuid) is not None:
                continue  # 虚拟相册（全部 / 回收站）不可写
            album = self._get_album(album_uuid)
            if album is not None:
                targets.append(album)
        return targets

    def _album_scope(self) -> str | None:
        """当前相册的 scope（``None`` = 物理相册）。"""
        if self._album_uuid is None:
            return None
        return self._scope_of(self._album_uuid)

    def _album_is_mutable(self) -> bool:
        """当前相册是否可写（物理相册才会出现在成员关系里）。"""
        return self._album_uuid is not None and self._album_scope() is None

    def _get_album(self, album_uuid: Any) -> Any | None:
        if self._view_service is None:
            return None
        try:
            return self._view_service.get_album(album_uuid)
        except Exception:  # noqa: BLE001
            logger.exception("content presenter: get_album failed: %s", album_uuid)
            return None

    # ---- 写操作 ----
    def move_asset_to_album(self, asset_uuid: str, target_uuid: Any) -> bool:
        """把资产挪到 ``target_uuid``。

        * 物理相册 → 真移动（``move_between_albums``，单事务，失败整体回滚）；
        * 虚拟「全部」视图 → 只能添加（``add_to_album``，资产仍留在原相册）。
        """
        if self._asset_ops is None or self._album_uuid is None:
            self.error.emit(self.tr("The file service is unavailable"))
            return False

        target = self._get_album(target_uuid)
        title = str(getattr(target, "title", "") or "")
        mutable = self._album_is_mutable()

        try:
            if mutable:
                result = self._asset_ops.move_between_albums(
                    self._album_uuid, target_uuid, [asset_uuid])
            else:
                if self._album_scope() != "active":
                    self.error.emit(self.tr("Album actions are unavailable in the trash view"))
                    return False
                result = self._asset_ops.add_to_album(
                    target_uuid, [asset_uuid])
        except Exception as exc:  # noqa: BLE001
            logger.exception("content presenter: album operation failed")
            self.error.emit(str(exc))
            return False

        changed = (
            getattr(result, "moved", ()) if mutable
            else getattr(result, "added", ())
        ) or ()
        if not changed:
            self.error.emit(
                self.tr("The asset is not in the current album; nothing was moved") if mutable
                else self.tr("Could not add to the album"))
            return False

        self._after_membership_change()
        if mutable:
            self.notice.emit(
                self.tr("Moved to “{album}”").format(album=title) if title
                else self.tr("Moved"))
        else:
            self.notice.emit(
                self.tr("Added to “{album}”").format(album=title) if title
                else self.tr("Added"))
        return True

    def remove_asset_from_album(self, asset_uuid: str) -> bool:
        """把资产从当前相册移除（只摘成员关系，不删资产）。"""
        if self._asset_ops is None or self._album_uuid is None:
            self.error.emit(self.tr("The file service is unavailable"))
            return False
        try:
            result = self._asset_ops.remove_from_album(
                self._album_uuid, [asset_uuid])
        except Exception as exc:  # noqa: BLE001
            logger.exception("content presenter: remove_from_album failed")
            self.error.emit(str(exc))
            return False
        removed = getattr(result, "removed", ()) or ()
        if not removed:
            self.error.emit(self.tr("The asset is not in the current album"))
            return False
        self._after_membership_change()
        self.notice.emit(self.tr("Removed from this album"))
        return True

    def create_album_and_move(self, asset_uuid: str) -> bool:
        """新建相册并把这格资产移进去（取消 / 空标题 = 什么都不做）。"""
        if self._album_service is None:
            self.error.emit(self.tr("The album service is unavailable"))
            return False
        title = self._ask_new_album_title()
        if not title:
            return False
        try:
            album = self._album_service.create(AlbumCreate(title=title))
        except Exception as exc:  # noqa: BLE001
            logger.exception("content presenter: create album failed")
            self.error.emit(str(exc))
            return False
        return self.move_asset_to_album(asset_uuid, album.uuid)

    def _ask_new_album_title(self) -> str:
        """弹「新建相册」标题输入框；取消 / 空输入返回空串。"""
        dialog = QInputDialog(self._window.content)
        dialog.setWindowTitle(self.tr("New album"))
        dialog.setLabelText(self.tr("Album title:"))
        if dialog.exec() != QInputDialog.DialogCode.Accepted:
            return ""
        return dialog.textValue().strip()

    def _after_membership_change(self) -> None:
        """成员关系变了：重拉当前列表并通知相册封面刷新。"""
        self.reload()
        self.assets_changed.emit()

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_unit_right_clicked(self, index: int) -> None:
        if not 0 <= index < len(self._uids):
            return
        menu = self.build_asset_menu(self._uids[index])
        menu.exec(QCursor.pos())

    def _on_visible_range(self, start: int, end: int, _request: int) -> None:
        self._visible = (int(start), int(end))
        self._load_rows(range(int(start), int(end) + 1))

    def _on_thumb_loaded(self, token: str, pixmap: QPixmap) -> None:
        if not token.startswith(TOKEN_PREFIX):
            return
        asset_uuid = token[len(TOKEN_PREFIX):]
        try:
            row = self._uids.index(asset_uuid)
        except ValueError:
            return
        # 网格格子是正方形：先把非方缩略图居中裁方，再交给委托铺满，
        # 否则默认委托会把图拉伸变形（缩略图文件本身保持原始宽高比）。
        self._model.set_pixmap(row, square_pixmap(pixmap, edge=GRID_EDGE))

    def _on_thumbnail_ready(self, _sender: object, *, asset_uuid: str) -> None:
        """缩略图生成完成（可能在别的 presenter 触发的）：补一次加载。"""
        try:
            row = self._uids.index(str(asset_uuid))
        except ValueError:
            return
        self._thumb_requested.discard(str(asset_uuid))
        if self._model.pixmap_at(row) is None:
            self._ensure_row(row)

    def _on_unit_activated(self, index: int) -> None:
        if 0 <= index < len(self._uids):
            self.asset_activated.emit(self._uids[index])

    def _on_unit_previewed(self, index: int) -> None:
        """空格：快速预览这一格（与回车 / 点击的「详情」分开的两条出口）。"""
        if 0 <= index < len(self._uids):
            self.asset_previewed.emit(self._uids[index])

    def _on_cursor_changed(self, index: int) -> None:
        if 0 <= index < len(self._uids):
            self.selection_changed.emit(self._uids[index])
        else:
            self.selection_changed.emit("")

    def _emit_current_selection(self) -> None:
        """把「光标所在资产」再广播一次（模型换源后补发）。

        视图在模型重置后把光标挪到合法位置（行数不足时收敛到末行、无数据
        时清空），但 ``cursor_changed`` 未必随换相册 / 过滤再发一次：不补这
        一发，标签栏就停在上一个资产（或一直是空）上，看起来「标签栏不会根
        据当前资产刷新」。
        """
        self.selection_changed.emit(self.current_asset_uuid() or "")



# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_content_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("content presenter skipped: no window bound")
        return None
    return ContentPresenter(container, window=window, parent=main)
