#
""""""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Optional
from uuid import UUID


@dataclass
class Album:
    id: int
    uuid: UUID
    title: str
    description: str
    cover: Optional[UUID]  # cover asset id
    type: int
    created_at: str
    modified_at: str


@dataclass
class AssetDTO:
    """资产 DTO，对应 assets 表；不包含软删除等内部字段。"""

    # 必填业务字段
    uuid: str
    file_path: str
    original_name: str
    mime_type: str
    file_hash: str

    # 可选 / 默认字段
    # id: Optional[int] = None
    source_id: int = -1

    thumb_path: Optional[str] = None
    thumb_small_path: Optional[str] = None
    thumb_medium_path: Optional[str] = None

    file_size: int = 0
    width: int = 0
    height: int = 0

    taken_at: Optional[str] = None
    city: Optional[str] = None
    exif: Optional[dict[str, Any]] = None

    is_favorite: bool = False

    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "AssetDTO":
        """从 sqlite3.Row、dict 或 Mapping 构造 DTO。"""
        data = dict(row)

        exif = None
        raw_exif = data.get("exif_json")
        if raw_exif:
            try:
                # noinspection bad-argument-type
                exif = json.loads(raw_exif)
            except (TypeError, json.JSONDecodeError):
                exif = None

        return cls(
            # id=data.get("id"),
            uuid=data["uuid"],
            file_path=data["file_path"],
            source_id=data.get("source_id", -1),
            thumb_path=data.get("thumb_path"),
            thumb_small_path=data.get("thumb_small_path"),
            thumb_medium_path=data.get("thumb_medium_path"),
            original_name=data["original_name"],
            mime_type=data["mime_type"],
            file_hash=data["file_hash"],
            file_size=data.get("file_size", 0) or 0,
            width=data.get("width", 0) or 0,
            height=data.get("height", 0) or 0,
            taken_at=data.get("taken_at"),
            city=data.get("city"),
            exif=exif,
            is_favorite=bool(data.get("is_favorite", 0)),
            created_at=data.get("created_at"),
            modified_at=data.get("modified_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
