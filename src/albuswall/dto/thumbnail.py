#
""""""

from sqlite3 import Row
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from albuswall.common.enums import ThumbSpec
from albuswall.utils.path import join_path

from .utils import to_uuid, row_get

# --------------------------------------------------------------------------- #
# 唯一权威定义：spec / column 映射
# --------------------------------------------------------------------------- #
# 仓储、服务、迁移脚本一律从这里 import，禁止就地重定义。

ALL_SPECS: tuple[ThumbSpec, ...] = (
    ThumbSpec.SMALL, ThumbSpec.MEDIUM, ThumbSpec.LARGE,
)

# spec → assets 列名
SPEC_TO_COLUMN: dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "thumb_small_path",
    ThumbSpec.MEDIUM: "thumb_medium_path",
    ThumbSpec.LARGE: "thumb_large_path",
}

COLUMN_TO_SPEC: dict[str, ThumbSpec] = {v: k for k, v in SPEC_TO_COLUMN.items()}

# 缩略图主目录列，独立于 spec
BASE_COLUMN: str = "thumb_path"

# 所有缩略图列（含 base），顺序固定：base 在前，spec 按 ALL_SPECS 顺序
ALL_THUMB_COLUMNS: tuple[str, ...] = (BASE_COLUMN, *(SPEC_TO_COLUMN[s] for s in ALL_SPECS))


# --------------------------------------------------------------------------- #
# 仓储返回结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ThumbnailPaths:
    """一个资产的缩略图路径集合。

    契约（与 schema 一致）：
        base  —— assets.thumb_path 列的原始值，缩略图主目录的**绝对路径**
        small/medium/large —— **相对 base** 的 spec 子路径

    完整路径 = base.rstrip('/') + '/' + spec_rel
    任一为空 → resolve() 返回 None。

    唯一权威来源：本类是缩略图路径的 canonical 表示。
    ``AssetDTO.thumb_path`` / ``thumb_<spec>_path`` 是历史镜像
    （见 albuswall.dto.album.AssetDTO 的 legacy 注释），
    仅用于向后兼容，新代码一律从这里取路径。
    """
    base: Optional[str] = None
    small: Optional[str] = None
    medium: Optional[str] = None
    large: Optional[str] = None

    def items(self):
        """迭代 (spec 名, 相对路径) 对，顺序由 ALL_SPECS 决定。

        只产出 spec 子路径，不含 base —— base 由调用方单独处理
        （见 source_trash._remove_thumbnail_files）。
        """
        for spec in ALL_SPECS:
            yield spec.value, self.for_spec(spec)

    def for_spec(self, spec: "ThumbSpec | str") -> Optional[str]:
        """取某个 spec 的相对路径；未知 spec 返回 None。"""
        key = spec.value if isinstance(spec, ThumbSpec) else spec
        if key == ThumbSpec.SMALL.value:
            return self.small
        if key == ThumbSpec.MEDIUM.value:
            return self.medium
        if key == ThumbSpec.LARGE.value:
            return self.large
        return None

    def resolve(self, spec: "ThumbSpec | str") -> Optional[str]:
        """把 (base, spec 相对路径) 拼成完整路径。任一为空则返回 None。

        仓储不碰文件系统，这只是给调用方一个统一的拼接口径。
        """
        return join_path(self.base, self.for_spec(spec))

    def as_dict(self) -> dict[str, Optional[str]]:
        return {
            "base": self.base,
            "small": self.small,
            "medium": self.medium,
            "large": self.large,
        }

    @classmethod
    def from_row(cls, row: Row) -> "ThumbnailPaths":
        """从含 thumb_path / thumb_*_path 列的行构造。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        用 ``row_get`` 容错：迁移期不少 SELECT 只挑了部分 spec 列
        （甚至只挑 thumb_path），缺列按 None 处理，不抛 KeyError。
        """
        return cls(
            base=row_get(row, "thumb_path"),
            small=row_get(row, "thumb_small_path"),
            medium=row_get(row, "thumb_medium_path"),
            large=row_get(row, "thumb_large_path"),
        )

    @classmethod
    def from_asset_dto(cls, dto: Any) -> "ThumbnailPaths":
        """【迁移专用，勿在新代码使用】从历史 ``AssetDTO`` 借壳构造。

        待 AssetDTO 的 thumb* 字段彻底移除后，本类方法一并删除。
        """
        return cls(
            base=getattr(dto, "thumb_path", None),
            small=getattr(dto, "thumb_small_path", None),
            medium=getattr(dto, "thumb_medium_path", None),
            large=getattr(dto, "thumb_large_path", None),
        )


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
    """count_by_status 的返回结构。"""
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


# --------------------------------------------------------------------------- #
# 任务/结果 DTO
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ThumbnailTask:
    """一次缩略图生成任务的输入。

    契约：``uuid`` 对外统一为 ``UUID``；构造点负责 ``str→UUID``
    （本类无 ``from_row``，由 Scanner / 调度侧构造）。
    """
    asset_id: int
    uuid: UUID
    source_id: int
    file_path: str
    file_hash: str
    source_path: str  # Scanner 补齐


# ---------------------------------------------------------------------- #
# 服务层输入
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ThumbnailTaskInput:
    """单个缩略图任务所需的全部上下文。

    由 ThumbnailRepository.get_task_input(asset_uuid)
    或 ThumbnailRepository.get_task_input_by_uuid(uuid) 构造。

    契约：``uuid`` 对外统一为 ``UUID``；``from_row`` 负责 ``str→UUID``。
    """
    asset_id: int
    uuid: UUID
    source_id: Optional[int]
    source_path: Optional[str]  # sources 表里的根目录/基础路径
    file_path: str  # assets 表里的相对路径

    @classmethod
    def from_row(cls, row: Row) -> "ThumbnailTaskInput":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。

        ``asset_id`` 优先取 ``id`` 列，缺失时回退到 ``asset_uuid``
        （历史调用点兼容，待调用方统一后收紧）。
        """
        return cls(
            asset_id=row_get(row, "id") or row_get(row, "asset_uuid"),
            uuid=to_uuid(row["uuid"]),
            source_id=row_get(row, "source_id"),
            source_path=row_get(row, "source_path"),
            file_path=row_get(row, "file_path"),
        )


# ---------------------------------------------------------------------- #
# 服务层输出
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """_work_one() 的执行结果。

    ok          —— 是否成功
    asset_uuid  —— 对应资产 uuid（契约：``UUID``，与 ``AssetDTO.uuid`` 同口径）
    error       —— 失败标记，形如 "render:UnidentifiedImageError"
    duration_ms —— 成功耗时（毫秒）
    retryable   —— False 表示重试没有意义（资产已删、源丢失、非法图片）；
                   True 表示临时故障（IO、OOM、写盘失败），可有限重试。
                   TaskService 应以此决定是否入重试队列。
    """
    ok: bool
    asset_uuid: UUID
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    retryable: bool = False
