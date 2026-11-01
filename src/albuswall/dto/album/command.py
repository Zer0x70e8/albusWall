#
"""Album 域 —— 命令侧 DTO（Command）。

归属见 ``dto/CONVENTIONS.md``：写模型（入参）放 ``command`` 侧，读模型留
``dto/album.py``。两边**不互相 import**。

命名与语义
----------
* :class:`AlbumCreate`    —— 建相簿；``title`` 必填，其余可省；
* :class:`AlbumUpdate`    —— PATCH 语义：没给字段就是"不改"，用
  :data:`~albuswall.dto.sentinel.UNSET` 表达"未提供"（与 ``TagUpdate`` 一致）；
  ``clear_cover=True`` 表示"把封面清空"，与 omit 严格区分；
* :class:`AlbumReorder`   —— 相簿列表的手动排序（``albums.sort_order``）。

为什么 ``cover_uuid`` 用 ``UNSET`` 三态而不是 ``Optional[UUID]``
--------------------------------------------------------------
``None`` 在 JSON 里是合法值，无法区分"没传"和"传了 null 表示清空"。三态
（``UNSET`` / ``UUID`` / ``None``）把"不动 / 设为某张 / 清空"表达完整，正是
``cover_asset_id`` 可空所需。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence
from uuid import UUID

from albuswall.dto.sentinel import UNSET
from albuswall.dto.utils import to_uuid

__all__ = ["AlbumCreate", "AlbumReorder", "AlbumUpdate"]

_UUIDLike = UUID | str

#: ``albums.album_type`` 的默认值（0 = 普通相簿）。
ALBUM_TYPE_NORMAL: int = 0


@dataclass(frozen=True, slots=True)
class AlbumCreate:
    """建相簿的入参。"""

    title: str
    description: str = ""
    album_type: int = ALBUM_TYPE_NORMAL
    sort_order: int = 0
    cover_uuid: Optional[_UUIDLike] = None

    def __post_init__(self) -> None:
        title = (self.title or "").strip()
        if not title:
            raise ValueError("album title must be non-empty")
        object.__setattr__(self, "title", title)


@dataclass(frozen=True, slots=True)
class AlbumUpdate:
    """相簿 PATCH 入参（``UNSET`` = 不修改）。

    ``cover_uuid`` 三态：

    * ``UNSET``      —— 不动封面；
    * ``UUID | str`` —— 改成这张资产（资产必须可见，否则仓储存 NULL）；
    * ``None``       —— 清空封面。
    """

    title: object = UNSET
    description: object = UNSET
    album_type: object = UNSET
    sort_order: object = UNSET
    cover_uuid: object = UNSET

    def is_empty(self) -> bool:
        return (
            self.title is UNSET
            and self.description is UNSET
            and self.album_type is UNSET
            and self.sort_order is UNSET
            and self.cover_uuid is UNSET
        )

    @property
    def derived_title(self) -> Optional[str]:
        """归一化后的新标题（``UNSET`` → ``None``；空串视为非法）。"""
        if self.title is UNSET:
            return None
        value = (str(self.title) or "").strip()
        if not value:
            raise ValueError("album title must be non-empty")
        return value


@dataclass(frozen=True, slots=True)
class AlbumReorder:
    """一次重排：按给定顺序重写 ``sort_order``（第 i 个 → i）。"""

    album_uuids: Sequence[_UUIDLike]

    def __post_init__(self) -> None:
        if not self.album_uuids:
            raise ValueError("album_uuids must be non-empty")
        # 早失败：非法 uuid 不该等到写库时才发现（那时可能已写了一半）。
        object.__setattr__(
            self, "album_uuids",
            tuple(to_uuid(u) for u in self.album_uuids),
        )
