#
""""""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import ClassVar, Optional

from albuswall.common.enums import CandidateStatus


@dataclass(frozen=True, slots=True)
class AssetCandidateCacheDTO:
    """asset_candidate_cache 表的数据传输对象。

    路径语义（与 media_library_schema.sql 一致）：
        path 为相对 ingest_source.source_path 的相对路径。
    """

    # ---- 必填 ----
    uuid: str
    path: str
    source_id: int

    # ---- 有默认值（与 schema DEFAULT 对齐）----
    mime_type: str = "application/octet-stream"
    status: CandidateStatus = CandidateStatus.PENDING

    # ---- 数据库生成 / 抢占字段 ----
    id: Optional[int] = None
    created_at: Optional[str] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None

    # 与 asset_candidate_cache 列顺序完全一致；
    # SELECT 列清单、INSERT 列清单都从这里派生，避免再手写一遍。
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "id", "uuid", "path", "source_id", "mime_type",
        "created_at", "status", "claimed_by", "claimed_at",
    )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AssetCandidateCacheDTO":
        """只接受 sqlite3.Row（connector 已设置 row_factory）。

        不再保留 tuple 顺序兼容分支：那份顺序和 COLUMNS 是双份维护，
        字段增删必炸。
        """
        return cls(
            id=row["id"],
            uuid=row["uuid"],
            path=row["path"],
            source_id=row["source_id"],
            mime_type=row["mime_type"],
            created_at=row["created_at"],
            status=CandidateStatus(row["status"]),
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
        )


@dataclass(frozen=True, slots=True)
class AssetCreateDTO:
    """assets 表插入用的 DTO。

    路径语义（与 media_library_schema.sql 一致）：
        file_path：相对路径。
          - source_id = 0：POSIX 相对 '/'，或 Windows 带盘符形式
          - 其它 source_id：相对 ingest_source.source_path
        thumb_path：缩略图主目录的绝对路径
        thumb_small/medium/large_path：相对 thumb_path 的子路径
    """

    # ---- 必填 ----
    uuid: str
    file_path: str
    original_name: str
    mime_type: str
    file_hash: str

    # ---- 有默认值 ----
    source_id: int = 0  # 0 = 手动导入虚拟根，与 schema DEFAULT 一致
    file_size: int = 0
    width: int = 0
    height: int = 0

    thumb_path: Optional[str] = None
    thumb_small_path: Optional[str] = None
    thumb_medium_path: Optional[str] = None
    thumb_large_path: Optional[str] = None

    taken_at: Optional[str] = None
    city: Optional[str] = None
    exif_json: Optional[str] = None

    is_favorite: int = 0
    is_deleted: int = 0
    deleted_at: Optional[str] = None

    # 与 assets 列顺序完全一致（不含 id / created_at / modified_at）
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "uuid", "file_path", "source_id",
        "thumb_path", "thumb_small_path", "thumb_medium_path", "thumb_large_path",
        "original_name", "mime_type", "file_hash",
        "file_size", "width", "height",
        "taken_at", "city", "exif_json",
        "is_favorite", "is_deleted", "deleted_at",
    )

    def to_insert_params(self) -> tuple:
        """按 COLUMNS 顺序生成 INSERT 参数，替代 _asset_params。"""
        return tuple(getattr(self, col) for col in self.COLUMNS)
