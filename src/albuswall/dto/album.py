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
    """资产 DTO，对应 assets 表；不包含软删除等内部字段。

    缩略图字段说明：
        本 DTO 如实映射 assets 表上的缩略图列：

            thumb_path            —— 缩略图主目录的绝对路径（base）
            thumb_<spec>_path     —— 相对 base 的 spec 子路径

        这些列对应 ThumbnailPaths 的 base / small / medium / large。
        拼接完整路径、按 spec 取路径等逻辑，请统一走
        ``ThumbnailRepository`` / ``ThumbnailPaths``，不要从本 DTO 的
        thumb* 字段自行拼接：

            # 推荐
            paths = thumb_repo.get_paths(asset_uuid)
            full  = paths.resolve(ThumbSpec.MEDIUM)

            # 不推荐：自行拼接，容易与仓储的路径口径脱节
            full = f"{dto.thumb_path}/{dto.thumb_medium_path}"
    """

    # 必填业务字段
    uuid: str
    file_path: str
    original_name: str
    mime_type: str
    file_hash: str

    # 可选 / 默认字段
    # id: Optional[int] = None  # 内部资产入库逻辑
    source_id: int = -1

    # NOTE: 职责问题，外部自己转化 enum
    # （thumb_path = 绝对 base；thumb_<spec>_path = 相对文件名）
    thumb_path: Optional[str] = None
    thumb_small_path: Optional[str] = None
    thumb_medium_path: Optional[str] = None
    thumb_large_path: Optional[str] = None

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
            # legacy, 请改用 ThumbnailRepository/ThumbnailPaths
            # 保留读取：向后兼容旧调用点，待迁移完成后再评估移除
            thumb_path=data.get("thumb_path"),
            thumb_small_path=data.get("thumb_small_path"),
            thumb_medium_path=data.get("thumb_medium_path"),
            thumb_large_path=data.get("thumb_large_path"),
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
