#
"""Tag 域 —— 查询侧 DTO（Query）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* 本模块只放**读模型与视图 DTO**，只由读路径（repo 读方法 → service 视图 → UI）产生与消费；
* 写模型（``TagCreate`` / ``TagUpdate``）在 ``dto/tag/command.py``，本模块禁止 import 它；
* 命令侧允许"写前读"本模块的 ``Tag``（单向白名单），但不得把读模型当命令入参再传回 repo。
"""

from __future__ import annotations

from sqlite3 import Row
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from albuswall.dto.utils import to_uuid

__all__ = [
    "Tag",
    "TagMatch",
    "TagTreeNode",
    "TagWithStats",
]


@dataclass
class Tag:
    """读模型：如实映射 tags 行。

    契约：``uuid`` / ``parent_uuid`` 对外为 ``UUID``；
    ``from_row`` 负责 ``str→UUID``。
    """
    uuid: UUID
    parent_uuid: Optional[UUID]
    name: str
    norm_name: str
    display_name: Optional[str]
    description: Optional[str]
    sort_order: int
    is_deleted: bool
    deleted_at: Optional[str]
    created_at: str
    modified_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Tag":
        """从 ``sqlite3.Row`` 构造。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        """
        raw_parent = row["parent_uuid"]
        return cls(
            uuid=to_uuid(row["uuid"]),
            parent_uuid=(
                to_uuid(raw_parent) if raw_parent is not None else None
            ),
            name=row["name"],
            norm_name=row["norm_name"],
            display_name=row["display_name"],
            description=row["description"],
            sort_order=row["sort_order"],
            is_deleted=bool(row["is_deleted"]),
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )


@dataclass(frozen=True, slots=True)
class TagTreeNode:
    """标签树节点视图（Query / 视图 DTO）。

    由 ``TagService`` 构建，repo 不产生；作为 service ↔ presenter 的视图契约，
    不属于持久化模型。**单一来源是本模块**：``services/tag.py`` 的同名副本已下沉，
    禁止再在任何层声明第二份。
    """
    tag: Tag
    children: tuple["TagTreeNode", ...] = ()
    depth: int = 0
    # 从根到当前的 display_name 链（不含自身）；rename 时不用重建
    path: tuple[str, ...] = ()

    @property
    def uuid(self) -> UUID:
        return self.tag.uuid

    @property
    def is_leaf(self) -> bool:
        return not self.children


@dataclass(frozen=True, slots=True)
class TagWithStats:
    """标签 + 统计的列表视图（Query / 视图 DTO），列表页用。"""
    tag: Tag
    asset_count: int  # 直接挂在该标签上的可见资产数
    child_count: int  # 直接子标签数
    subtree_asset_count: int  # 含子孙的资产数（仅 include_subtree=True 时计算）


@dataclass(frozen=True, slots=True)
class TagMatch:
    """标签搜索结果：标签本体 + 作用域内资产数 + 完整路径（Query / 搜索视图）。"""
    tag: Tag
    asset_count: int
    path: str  # 从根到自身的展示路径，如 "旅行/日本/京都"

    @property
    def uuid(self) -> UUID:
        return self.tag.uuid

    @property
    def label(self) -> str:
        return self.path or self.tag.display_name or self.tag.name
