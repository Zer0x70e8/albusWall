#
"""相册格子模型。

相册列表（封面 + 标题 + 描述 + 资产数）的 Qt 展示模型。数据源可以是
``albuswall.dto.album.Album``、任何同名属性的对象，也可以是本模块的
:class:`AlbumEntry` —— 模型只按字段名取值，不 import 领域层。

封面是派生数据：解码由调用方负责（线程池 / ``ImageLoader`` 都行），
只把 ``QPixmap`` 交给 :meth:`AlbumModel.set_pixmap`，避免模型里出现 IO。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QPixmap

from .base import EntryListModel, attr_of
from .roles import AlbumRole

__all__ = ["AlbumEntry", "AlbumModel"]


@dataclass(slots=True)
class AlbumEntry:
    """一行相册格子。

    字段名与 :data:`AlbumRole` 一一对应；``uuid`` 用 ``str`` 存，但任何
    可 ``str()`` 的标识都行（模型回放时原样返回）。
    """

    uuid: str
    title: str = ""
    description: str = ""
    pixmap: QPixmap | None = None
    asset_count: int = 0

    # ---- 构造 ----
    @classmethod
    def from_object(cls, album: Any) -> "AlbumEntry":
        """从任意「带同名属性的相册对象」构造（DTO / Row / dict 均可）。"""
        return cls(
            uuid=_as_text(attr_of(album, "uuid")),
            title=_as_text(attr_of(album, "title") or attr_of(album, "name")),
            description=_as_text(attr_of(album, "description")),
            asset_count=int(attr_of(album, "asset_count") or 0),
        )


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


class AlbumModel(EntryListModel[AlbumEntry]):
    """相册格子列表模型。

    Roles（见 :class:`~albuswall.ui.model.roles.ItemRole`）::

        DisplayRole / ToolTipRole   → 标题
        DecorationRole              → 封面 ``QPixmap``
        AlbumRole.Uuid              → 标识
        AlbumRole.Title             → 标题
        AlbumRole.Description       → 描述
        AlbumRole.Pixmap            → 封面
        AlbumRole.AssetCount        → 资产数量
    """

    ROLE_FIELDS = {
        AlbumRole.Uuid: "uuid",
        AlbumRole.Title: "title",
        AlbumRole.Description: "description",
        AlbumRole.Pixmap: "pixmap",
        AlbumRole.AssetCount: "asset_count",
    }
    DISPLAY_FIELD = "title"
    TOOLTIP_FIELD = "description"
    DECORATION_FIELD = "pixmap"

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def set_albums(self, albums: Iterable[Any]) -> None:
        """用任意相册对象序列重建列表（DTO / ``AlbumEntry`` 都行）。"""
        self.set_entries(self.to_entries(albums))

    @staticmethod
    def to_entries(albums: Iterable[Any]) -> list[AlbumEntry]:
        """把混合序列规整成 ``AlbumEntry``；已经是条目的原样保留。"""
        entries: list[AlbumEntry] = []
        for album in albums:
            if isinstance(album, AlbumEntry):
                entries.append(album)
            else:
                entries.append(AlbumEntry.from_object(album))
        return entries

    # ------------------------------------------------------------------ #
    # 便捷 API
    # ------------------------------------------------------------------ #
    def append_album(
            self,
            uuid: Any,
            title: str = "",
            description: str = "",
            pixmap: QPixmap | None = None,
            asset_count: int = 0,
    ) -> int:
        """追加一行相册，返回其行号。"""
        return self.append(AlbumEntry(
            uuid=_as_text(uuid),
            title=title,
            description=description,
            pixmap=pixmap,
            asset_count=int(asset_count),
        ))

    def set_pixmap(self, row: int, pixmap: QPixmap | None) -> None:
        """单独回填封面（解码完成后再更新，避免整表 reset）。"""
        entry = self.entry_at(row)
        if entry is None or entry.pixmap is pixmap:
            return
        entry.pixmap = pixmap
        self.touch_row(
            row, (AlbumRole.Pixmap, Qt.ItemDataRole.DecorationRole))

    def set_cover_for(self, uuid: Any, pixmap: QPixmap | None) -> bool:
        """按标识回填封面；找不到返回 ``False``。"""
        row = self.row_of(uuid)
        if row < 0:
            return False
        self.set_pixmap(row, pixmap)
        return True

    def uuid_at(self, row: int) -> str:
        entry = self.entry_at(row)
        return entry.uuid if entry is not None else ""

    def row_of(self, uuid: Any) -> int:
        """返回标识对应的行号；找不到时返回 ``-1``。"""
        target = _as_text(uuid)
        for row, entry in enumerate(self._entries):
            if entry.uuid == target:
                return row
        return -1

    def index_of(self, uuid: Any) -> QModelIndex:
        """返回标识对应的索引；找不到时返回无效索引。"""
        row = self.row_of(uuid)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def pixmap_at(self, row: int) -> QPixmap | None:
        entry = self.entry_at(row)
        return entry.pixmap if entry is not None else None
