#
""""""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Any


def _get(row: Any, key: str, default: Any = None) -> Any:
    """兼容 sqlite3.Row / dict / Mapping 的取值。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


class ThumbSpec(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# --------------------------------------------------------------------------- #
# 仓储返回结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ThumbnailPaths:
    """一个资产的缩略图相对路径集合。

    base 是缩略图主目录（相对 assets.thumb_path），其余三项是相对 base 的子路径。
    完整路径 = base + "/" + spec 相对路径。
    """
    base: Optional[str] = None
    small: Optional[str] = None
    medium: Optional[str] = None
    large: Optional[str] = None

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
        rel = self.for_spec(spec)
        if not rel:
            return None
        if rel.startswith("/"):
            return rel  # 兜底：已经是绝对路径
        if not self.base:
            return None
        return f"{self.base.rstrip('/')}/{rel.lstrip('/')}"

    def as_dict(self) -> dict[str, Optional[str]]:
        return {
            "base": self.base,
            "small": self.small,
            "medium": self.medium,
            "large": self.large,
        }

    @classmethod
    def from_row(cls, row: Mapping) -> "ThumbnailPaths":
        """从含 thumb_path / thumb_*_path 列的行构造。"""
        return cls(
            base=row["thumb_path"],
            small=row["thumb_small_path"],
            medium=row["thumb_medium_path"],
            large=row["thumb_large_path"],
        )


@dataclass(frozen=True, slots=True)
class MissingThumbnailRow:
    """list_missing 的返回行：待补齐缩略图的活跃资产。"""
    id: int
    uuid: str
    source_id: Optional[int]
    file_path: str
    file_hash: str
    paths: ThumbnailPaths

    @classmethod
    def from_row(cls, row: Mapping) -> "MissingThumbnailRow":
        return cls(
            id=row["id"],
            uuid=row["uuid"],
            source_id=row["source_id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            paths=ThumbnailPaths.from_row(row),
        )


@dataclass(frozen=True, slots=True)
class ThumbnailHashRow:
    """list_all_with_hashes 的返回行：用于与磁盘反向比对。"""
    id: int
    uuid: str
    file_hash: str
    paths: ThumbnailPaths

    @classmethod
    def from_row(cls, row: Mapping) -> "ThumbnailHashRow":
        return cls(
            id=row["id"],
            uuid=row["uuid"],
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
    """一次缩略图生成任务的输入。"""
    asset_id: int
    uuid: str
    source_id: int
    file_path: str
    file_hash: str
    source_path: str  # Scanner 补齐


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    ok: bool
    asset_id: int
    error: Optional[str] = None
    duration_ms: int = 0


# ---------------------------------------------------------------------- #
# 服务层输入
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ThumbnailTaskInput:
    """单个缩略图任务所需的全部上下文。

    由 ThumbnailRepository.get_task_input(asset_id)
    或 ThumbnailRepository.get_task_input_by_uuid(uuid) 构造。
    """
    asset_id: int
    uuid: str
    source_id: Optional[int]
    source_path: Optional[str]  # sources 表里的根目录/基础路径
    file_path: str  # assets 表里的相对路径

    @classmethod
    def from_row(cls, row: Any) -> "ThumbnailTaskInput":
        return cls(
            asset_id=_get(row, "id") or _get(row, "asset_id"),
            uuid=_get(row, "uuid"),
            source_id=_get(row, "source_id"),
            source_path=_get(row, "source_path"),
            file_path=_get(row, "file_path"),
        )


# ---------------------------------------------------------------------- #
# 服务层输出
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """_work_one() 的执行结果。

    ok         —— 是否成功（源文件不存在等“无需重试”的场景返回 False 而不抛异常）
    asset_id   —— 对应资产 id
    error      —— 失败时的错误标记，形如 "render:UnidentifiedImageError"
    duration_ms—— 成功时的耗时（毫秒）
    """
    ok: bool
    asset_id: int
    error: Optional[str] = None
    duration_ms: Optional[int] = None
