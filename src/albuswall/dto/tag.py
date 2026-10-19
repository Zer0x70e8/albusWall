#
""""""

from dataclasses import dataclass, field, fields
from typing import Optional

from albuswall.dto.sentinel import UNSET, PatchField
from albuswall.utils.text import normalize_tag_name


@dataclass
class Tag:
    """读模型：如实映射 tags 行。"""
    uuid: str
    parent_uuid: Optional[str]
    name: str
    norm_name: str
    display_name: Optional[str]
    description: Optional[str]
    sort_order: int
    is_deleted: bool
    deleted_at: Optional[str]
    created_at: str
    modified_at: str


@dataclass
class TagCreate:
    """创建输入：只有用户能提供的字段。"""
    name: str
    parent_uuid: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    sort_order: int = 0
    # 派生字段：调用方不能传，构造后由 __post_init__ 写入
    norm_name: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tag name must be non-empty")
        self.norm_name = normalize_tag_name(self.name)


@dataclass
class TagUpdate:
    """PATCH 输入：UNSET 跳过，显式 None 置 NULL。"""
    name: PatchField[str] = UNSET
    display_name: PatchField[str] = UNSET
    description: PatchField[str] = UNSET
    sort_order: PatchField[int] = UNSET

    def __post_init__(self) -> None:
        if self.name is not UNSET and self.name is not None:
            # noinspection unresolved-references
            if not self.name.strip():
                raise ValueError("tag name must be non-empty")

    @property
    def derived_norm_name(self) -> Optional[str]:
        """name 有值时返回归一化结果；UNSET / None 返回 None。

        用 property 而非 field：不污染 ``is_empty()`` 的遍历，
        也不会让调用方误传 ``norm_name`` 进来。
        """
        if self.name is UNSET or self.name is None:
            return None
        # noinspection bad-argument-type
        return normalize_tag_name(self.name)

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is UNSET for f in fields(self))


# service
@dataclass(frozen=True)
class TagTreeNode:
    """UI 树节点。由 TagService 构建，repo 不产生。"""
    tag: Tag
    children: tuple["TagTreeNode", ...] = ()
    depth: int = 0
    # 路径缓存（如 "旅行/日本/京都"），rename 时不用重建
    path: str = ""

    @property
    def uuid(self) -> str: return self.tag.uuid

    @property
    def is_leaf(self) -> bool: return not self.children


@dataclass(frozen=True)
class TagWithStats:
    """列表/详情用的带统计视图。"""
    tag: Tag
    asset_count: int
    child_count: int
    subtree_asset_count: int = 0  # 含子孙，可选


@dataclass(frozen=True)
class TagSelection:
    """当前选中的标签集合，用于 tagbar 回显。"""
    uuids: tuple[str, ...] = ()
    primary_uuid: str | None = None
