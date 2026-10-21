#
""""""

from __future__ import annotations

import json
from sqlite3 import Row
from dataclasses import dataclass, asdict
from typing import Any, Optional
from uuid import UUID

from .utils import to_uuid, row_get

__all__ = ["Album", "AssetDTO"]


@dataclass(frozen=True, slots=True)
class Album:
    """db中拿的，db作为源头需要信任，所以不能修改。"""
    # id: int
    uuid: UUID
    title: str
    # 契约：Album.description 默认空字符串，不用 Optional[str]
    description: str = ""
    cover: Optional[UUID] = None  # cover asset id
    type: int = 0
    created_at: str = ""
    modified_at: str = ""

    @classmethod
    def from_row(cls, row: Row) -> "Album":
        """从 ``sqlite3.Row`` 构造。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        ``cover`` 由 SELECT 里 ``cover_uuid`` 列派生（封面 LEFT JOIN 的
        结果），该列缺失或为空都表示没有封面；其余可选列走 ``row_get``
        容忍轻查询只挑部分列。
        """
        raw_cover = row_get(row, "cover_uuid")
        return cls(
            uuid=to_uuid(row["uuid"]),
            title=row["title"],
            description=row_get(row, "description") or "",
            cover=to_uuid(raw_cover) if raw_cover else None,
            type=row_get(row, "type", 0) or 0,
            created_at=row_get(row, "created_at") or "",
            modified_at=row_get(row, "modified_at") or "",
        )


@dataclass(frozen=True, slots=True)
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
    # 契约：对外字段统一为 UUID；str→UUID 收敛在 from_row
    # id: Optional[int] = None  # 排序用
    uuid: UUID
    file_path: str
    original_name: str
    mime_type: str
    file_hash: str
    source_id: int

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
    # 生命周期态（软删除）
    # include_deleted=False 时正常查询永远是 False/None；
    # include_deleted=True（Trash / 恢复流程）时才会拿到真实值。
    is_deleted: bool = False

    deleted_at: Optional[str] = None
    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Row) -> "AssetDTO":
        """从 ``sqlite3.Row`` 构造 DTO。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        内部仍转为 ``dict`` 以保留轻查询缺列容错（部分 SELECT 只挑列）。
        """
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
            uuid=to_uuid(data["uuid"]),
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
            is_deleted=bool(data.get("is_deleted", 0)),
            deleted_at=data.get("deleted_at"),
            created_at=data.get("created_at"),
            modified_at=data.get("modified_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        """浅转换：只变 dict，``UUID`` / ``Enum`` 原样保留。

        契约：``to_dict`` 面向日志/调试，不承担 json 序列化责任。
        """
        return asdict(self)
