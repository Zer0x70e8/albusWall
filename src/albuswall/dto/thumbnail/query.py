#
"""Thumbnail 域 —— 查询侧 DTO（Query）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* 本模块只放**读路径返回的行 / 统计**：``list_missing`` → ``MissingThumbnailRow``、
  ``list_all_with_hashes`` → ``ThumbnailHashRow``、``count_by_status`` → ``ThumbnailStats``；
* 行级列表项允许用 ``*Row`` 后缀（与 ``*ListItem`` 等价，命名更贴近 SQL 视角）；
* 命令侧与查询侧共享的路径值对象在 ``dto/thumbnail/paths.py``。
"""

from __future__ import annotations

from sqlite3 import Row
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from albuswall.dto.utils import to_uuid

from .paths import ThumbnailPaths

__all__ = [
    "MissingThumbnailRow",
    "ThumbnailHashRow",
    "ThumbnailStats",
]


@dataclass(frozen=True, slots=True)
class MissingThumbnailRow:
    """list_missing 的返回行：待补齐缩略图的活跃资产。

    契约：``uuid`` 对外统一为 ``UUID``；``from_row`` 负责 ``str→UUID``。
    """
    id: int
    uuid: UUID
    source_id: Optional[int]
    file_path: str
    file_hash: str
    paths: ThumbnailPaths

    @classmethod
    def from_row(cls, row: Row) -> "MissingThumbnailRow":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。"""
        return cls(
            id=row["id"],
            uuid=to_uuid(row["uuid"]),
            source_id=row["source_id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            paths=ThumbnailPaths.from_row(row),
        )


@dataclass(frozen=True, slots=True)
class ThumbnailHashRow:
    """list_all_with_hashes 的返回行：用于与磁盘反向比对。

    契约：``uuid`` 对外统一为 ``UUID``；``from_row`` 负责 ``str→UUID``。
    """
    id: int
    uuid: UUID
    file_hash: str
    paths: ThumbnailPaths

    @classmethod
    def from_row(cls, row: Row) -> "ThumbnailHashRow":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。"""
        return cls(
            id=row["id"],
            uuid=to_uuid(row["uuid"]),
            file_hash=row["file_hash"],
            paths=ThumbnailPaths.from_row(row),
        )


@dataclass(frozen=True, slots=True)
class ThumbnailStats:
    """count_by_status 的返回结构（Query / ``*Stats``）。"""
    total: int = 0
    has_base: int = 0
    has_large: int = 0
    has_medium: int = 0
    has_small: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "has_base": self.has_base,
            "has_large": self.has_large,
            "has_medium": self.has_medium,
            "has_small": self.has_small,
        }
