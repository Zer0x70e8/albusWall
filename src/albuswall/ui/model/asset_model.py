#
"""资产格子模型。

主网格（相册内资产）与任何「一格一张图」的列表共用的展示模型。相比旧版的
相册模型，这里把**格子状态**也一起建模了：收藏、回收站倒计时、软删标记
—— 委托据此在缩略图上画角标，而不是让 presenter 去拼第二份数据。

缩略图解码同样不在这里做：调用方把 ``QPixmap`` 交给
:meth:`AssetGridModel.set_pixmap`。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QPixmap

from .base import EntryListModel, attr_of
from .roles import AssetRole

__all__ = ["AssetEntry", "AssetGridModel"]


@dataclass(slots=True)
class AssetEntry:
    """一格资产。

    ``favorite`` / ``days_left`` / ``deleted`` 允许是 ``None``，表示「未知」：
    委托遇到 ``None`` 就不画对应角标，不会把未知当成「未收藏」。
    """

    uuid: str
    title: str = ""
    pixmap: QPixmap | None = None
    favorite: bool | None = None
    days_left: int | None = None
    deleted: bool | None = None

    @classmethod
    def from_object(cls, asset: Any) -> "AssetEntry":
        """从任意「带同名属性的资产对象」构造（DTO / Row / dict 均可）。"""
        days_left = attr_of(asset, "days_left")
        if days_left is None:
            days_left = attr_of(asset, "days_remaining")
        favorite = attr_of(asset, "favorite")
        if favorite is None:
            favorite = attr_of(asset, "is_favorite")
        deleted = attr_of(asset, "deleted")
        if deleted is None:
            deleted = attr_of(asset, "is_deleted")
        title = attr_of(asset, "title") or attr_of(asset, "file_name")
        return cls(
            uuid="" if attr_of(asset, "uuid") is None else str(attr_of(asset, "uuid")),
            title="" if title is None else str(title),
            favorite=None if favorite is None else bool(favorite),
            days_left=None if days_left is None else int(days_left),
            deleted=None if deleted is None else bool(deleted),
        )


class AssetGridModel(EntryListModel[AssetEntry]):
    """资产格子列表模型。

    Roles（见 :class:`~albuswall.ui.model.roles.ItemRole`）::

        DisplayRole / ToolTipRole   → 标题
        DecorationRole              → 缩略图 ``QPixmap``
        AssetRole.Uuid              → 标识
        AssetRole.Title             → 标题
        AssetRole.Pixmap            → 缩略图
        AssetRole.Favorite          → 收藏状态（``None`` = 未知）
        AssetRole.DaysLeft          → 回收站剩余天数（``None`` = 不在回收站）
        AssetRole.Deleted           → 软删标记（``None`` = 未知）
    """

    ROLE_FIELDS = {
        AssetRole.Uuid: "uuid",
        AssetRole.Title: "title",
        AssetRole.Pixmap: "pixmap",
        AssetRole.Favorite: "favorite",
        AssetRole.DaysLeft: "days_left",
        AssetRole.Deleted: "deleted",
    }
    DISPLAY_FIELD = "title"
    TOOLTIP_FIELD = "title"
    DECORATION_FIELD = "pixmap"

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def set_assets(self, assets: Iterable[Any]) -> None:
        """用任意资产对象序列重建列表（DTO / ``AssetEntry`` 都行）。"""
        self.set_entries(self.to_entries(assets))

    @staticmethod
    def to_entries(assets: Iterable[Any]) -> list[AssetEntry]:
        entries: list[AssetEntry] = []
        for asset in assets:
            if isinstance(asset, AssetEntry):
                entries.append(asset)
            else:
                entries.append(AssetEntry.from_object(asset))
        return entries

    # ------------------------------------------------------------------ #
    # 按 index-based 数据源（``VirtualScrollWidget``）对齐的便捷接口
    # ------------------------------------------------------------------ #
    def append_asset(
            self,
            uuid: Any,
            title: str = "",
            pixmap: QPixmap | None = None,
            *,
            favorite: bool | None = None,
            days_left: int | None = None,
            deleted: bool | None = None,
    ) -> int:
        """追加一格资产，返回其行号。"""
        return self.append(AssetEntry(
            uuid="" if uuid is None else str(uuid),
            title=title,
            pixmap=pixmap,
            favorite=favorite,
            days_left=days_left,
            deleted=deleted,
        ))

    def set_pixmap(self, row: int, pixmap: QPixmap | None) -> None:
        """回填缩略图（异步加载完成时调用）。"""
        entry = self.entry_at(row)
        if entry is None or entry.pixmap is pixmap:
            return
        entry.pixmap = pixmap
        self.touch_row(
            row, (AssetRole.Pixmap, Qt.ItemDataRole.DecorationRole))

    def set_favorite(self, row: int, favorite: bool | None) -> None:
        """更新收藏状态并通知该行重绘。"""
        entry = self.entry_at(row)
        if entry is None or entry.favorite == favorite:
            return
        entry.favorite = favorite
        self.touch_row(row, (AssetRole.Favorite,))

    def set_days_left(self, row: int, days_left: int | None) -> None:
        """更新回收站倒计时并通知该行重绘。"""
        entry = self.entry_at(row)
        if entry is None or entry.days_left == days_left:
            return
        entry.days_left = days_left
        self.touch_row(row, (AssetRole.DaysLeft,))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def uuid_at(self, row: int) -> str:
        entry = self.entry_at(row)
        return entry.uuid if entry is not None else ""

    def uuids(self) -> list[str]:
        """当前全部标识（按行号顺序）。"""
        return [entry.uuid for entry in self._entries]

    def row_of(self, uuid: Any) -> int:
        """返回标识对应的行号；找不到时返回 ``-1``。"""
        target = "" if uuid is None else str(uuid)
        for row, entry in enumerate(self._entries):
            if entry.uuid == target:
                return row
        return -1

    def index_of(self, uuid: Any) -> QModelIndex:
        row = self.row_of(uuid)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def pixmap_at(self, row: int) -> QPixmap | None:
        entry = self.entry_at(row)
        return entry.pixmap if entry is not None else None
