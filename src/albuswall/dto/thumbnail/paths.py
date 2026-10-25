#
"""Thumbnail 域 —— 共享值对象（Paths）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：``ThumbnailPaths`` 是**共享值对象**，
既被查询路径返回（``get_paths*`` / ``list_missing``），也被命令路径当作
"删除前的路径快照"消费（``clear_and_snapshot*`` / ``update_paths``），
因此不判 Command / Query，单独成模块，两侧都允许 import，禁止各自复制一份。

唯一权威来源：本类是缩略图路径的 canonical 表示。``Asset.thumb_path`` /
``thumb_<spec>_path`` 是历史镜像（见 ``dto.album.Asset`` 的 legacy 注释），
仅用于向后兼容，新代码一律从这里取路径。
"""

from __future__ import annotations

from sqlite3 import Row
from dataclasses import dataclass
from typing import Any, Optional

from albuswall.common.constants.thumbnail import ALL_SPECS
from albuswall.common.enums import ThumbSpec
from albuswall.utils.path import join_path

from albuswall.dto.utils import row_get

__all__ = ["ThumbnailPaths"]


@dataclass(frozen=True, slots=True)
class ThumbnailPaths:
    """一个资产的缩略图路径集合（共享值对象）。

    契约（与 schema 一致）：
        base  —— assets.thumb_path 列的原始值，缩略图主目录的**绝对路径**
        small/medium/large —— **相对 base** 的 spec 子路径

    完整路径 = base.rstrip('/') + '/' + spec_rel
    任一为空 → resolve() 返回 None。
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
        """【迁移专用，勿在新代码使用】从历史 ``Asset`` 借壳构造。

        待 ``Asset`` 的 thumb* 字段彻底移除后，本类方法一并删除。
        """
        return cls(
            base=getattr(dto, "thumb_path", None),
            small=getattr(dto, "thumb_small_path", None),
            medium=getattr(dto, "thumb_medium_path", None),
            large=getattr(dto, "thumb_large_path", None),
        )
