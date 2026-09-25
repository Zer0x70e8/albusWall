#
""""""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AssetCandidateCacheDTO:
    """用于 asset_candidate_cache 表的数据传输对象，同时支持创建和读取"""
    uuid: str
    path: str
    source_id: int
    mime_type: Optional[str]
    id: Optional[int] = None  # 数据库自增主键，创建时无需提供
    created_at: Optional[str] = None  # 数据库自动填充，创建时通常忽略
    status: str = 'pending'  # pending / processing / done / failed
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None

    @classmethod
    def from_row(cls, row):
        if hasattr(row, 'keys'):  # sqlite3.Row
            return cls(
                id=row['id'],
                uuid=row['uuid'],
                path=row['path'],
                source_id=row['source_id'],
                mime_type=row['mime_type'],
                created_at=row['created_at'],
                status=row['status'],
                claimed_by=row['claimed_by'],
                claimed_at=row['claimed_at'],
            )
        # 兼容普通 tuple，假设列顺序为:
        # id, uuid, path, source_id, mime_type,
        # created_at, status, claimed_by, claimed_at
        return cls(
            id=row[0],
            uuid=row[1],
            path=row[2],
            source_id=row[3],
            mime_type=row[4],
            created_at=row[5] if len(row) > 5 else None,
            status=row[6] if len(row) > 6 else 'pending',
            claimed_by=row[7] if len(row) > 7 else None,
            claimed_at=row[8] if len(row) > 8 else None,
        )


@dataclass
class AssetCreateDTO:
    """用于创建 assets 表记录的 DTO"""

    # 必填字段（无默认值）
    uuid: str  # 唯一标识
    file_path: str  # 文件路径（相对或绝对，取决于 source_id）语义见 media_library_schema.sql
    original_name: str  # 原始文件名
    mime_type: str  # MIME 类型
    file_hash: str  # 文件哈希值

    # 可选字段（有默认值）
    file_size: int = 0  # 文件大小（字节）
    width: int = 0  # 图像宽度
    height: int = 0  # 图像高度
    source_id: Optional[int] = None  # 关联导入源 ID，手动导入可为空

    thumb_path: Optional[str] = None  # base
    # relative base path
    thumb_small_path: Optional[str] = None
    thumb_medium_path: Optional[str] = None
    thumb_large_path: Optional[str] = None

    taken_at: Optional[str] = None  # 拍摄时间（ISO 格式字符串）
    city: Optional[str] = None
    exif_json: Optional[str] = None  # EXIF 信息 JSON
    is_favorite: int = 0  # 是否收藏，0/1
    is_deleted: int = 0  # 是否已删除，0/1
    deleted_at: Optional[str] = None

    # 说明：id、created_at、modified_at 由数据库自动生成，无需在 DTO 中提供
