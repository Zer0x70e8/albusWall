#
""""""

from __future__ import annotations

from sqlite3 import Row
from dataclasses import dataclass
from typing import Any, ClassVar, Optional
from uuid import UUID

from albuswall.common.enums import CandidateStatus

from .utils import to_uuid

__all__ = ["AssetCreateDTO", "AssetCandidateCacheDTO"]


def _coerce_status(
        value: Any,
        default: CandidateStatus = CandidateStatus.PENDING,
) -> CandidateStatus:
    """读路径枚举容错：非法值降级到显式默认，不阻塞。

    契约：读路径遇非法枚举值降级到显式默认；写路径抛 ``ValueError``。
    """
    if isinstance(value, CandidateStatus):
        return value
    try:
        return CandidateStatus(value)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True, slots=True)
class AssetCandidateCacheDTO:
    """asset_candidate_cache 表的数据传输对象。

    路径语义（与 media_library_schema.sql 一致）：
        path 为相对 ingest_source.source_path 的相对路径。
    """

    # ---- 必填 ----
    # 契约：对外字段统一为 UUID；from_row 负责 str→UUID
    uuid: UUID
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
    def from_row(cls, row: Row) -> "AssetCandidateCacheDTO":
        """只接受 sqlite3.Row（connector 已设置 row_factory）。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        """
        return cls(
            id=row["id"],
            uuid=to_uuid(row["uuid"]),
            path=row["path"],
            source_id=row["source_id"],
            mime_type=row["mime_type"],
            created_at=row["created_at"],
            status=_coerce_status(row["status"]),
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
        )

    def to_insert_params(self) -> tuple:
        """按 COLUMNS 顺序生成 INSERT 参数。

        契约：写路径负责 UUID→str；``uuid`` 列转 ``str``，
        ``status`` 列取枚举 ``value``。``id`` 为 ``None`` 时由 SQLite
        AUTOINCREMENT 生成（绑定 NULL 即可）。
        """
        params = []
        for col in self.COLUMNS:
            value = getattr(self, col)
            if col == "uuid" and isinstance(value, UUID):
                value = str(value)
            elif col == "status" and isinstance(value, CandidateStatus):
                value = value.value
            params.append(value)
        return tuple(params)


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
    # 契约：对外字段统一为 UUID；写路径（to_insert_params）负责 UUID→str
    uuid: UUID
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

    # 契约：仅 AssetCreateDTO.is_favorite 保留 int（未来星级）
    is_favorite: int = 0
    # 契约：is_deleted 改 bool（DB 仍是 INTEGER 0/1，写入时归一）
    is_deleted: bool = False
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
        """按 COLUMNS 顺序生成 INSERT 参数，替代 _asset_params。

        契约：写路径负责 UUID→str；``is_deleted`` 由 bool 归一为 0/1，
        ``is_favorite`` 保持 int（未来星级）。
        """
        params = []
        for col in self.COLUMNS:
            value = getattr(self, col)
            if col == "uuid" and isinstance(value, UUID):
                value = str(value)
            elif col == "is_deleted":
                value = int(bool(value))
            elif col == "is_favorite":
                value = int(value)
            params.append(value)
        return tuple(params)
