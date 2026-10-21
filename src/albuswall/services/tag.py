#
"""标签业务服务。

职责：把 TagRepository 的歧义返回值翻译成显式异常 / 结构化结果，
让 presenter 只做 try/except，不需要了解 repo 的内部约定。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional, Sequence
from uuid import UUID

from albuswall.dto.sentinel import UNSET
from albuswall.dto.tag import Tag, TagCreate, TagUpdate
from albuswall.dto.utils import to_uuid
from albuswall.repositories.utils.types import UuidKeyed

if TYPE_CHECKING:
    from albuswall.repositories import TagRepository

_UUIDLike = UUID | str


# ============================== 错误 ==============================


class TagError(Exception):
    """Tag 业务错误基类。"""


class TagNotFound(TagError):
    def __init__(self, uuid: str):
        super().__init__(f"tag not found: {uuid}")
        self.uuid = uuid


class TagParentNotFound(TagError):
    def __init__(self, parent_uuid: str):
        super().__init__(f"parent tag not found: {parent_uuid}")
        self.parent_uuid = parent_uuid


class TagNameConflict(TagError):
    def __init__(
            self,
            name: str,
            existing_uuid: Optional[_UUIDLike] = None,
    ):
        super().__init__(f"tag name conflict: {name!r}")
        self.name = name
        self.existing_uuid = existing_uuid


class TagHasChildren(TagError):
    def __init__(self, uuid: str, children_uuids: Sequence[str]):
        super().__init__(f"tag has children: {uuid} -> {list(children_uuids)}")
        self.uuid = uuid
        self.children_uuids = tuple(children_uuids)


class TagCycle(TagError):
    def __init__(self, tag_uuid: str, new_parent_uuid: Optional[str]):
        super().__init__(f"cycle detected: {tag_uuid} -> {new_parent_uuid}")
        self.tag_uuid = tag_uuid
        self.new_parent_uuid = new_parent_uuid


class TagMoveConflict(TagError):
    def __init__(self, tag_uuid: str, new_parent_uuid: Optional[str]):
        super().__init__(
            f"cannot move tag {tag_uuid} to {new_parent_uuid}: name conflict"
        )
        self.tag_uuid = tag_uuid
        self.new_parent_uuid = new_parent_uuid


class TagRestoreConflict(TagError):
    def __init__(self, uuid: str):
        super().__init__(f"cannot restore tag (name conflict): {uuid}")
        self.uuid = uuid


# ============================== 结果 DTO ==============================


@dataclass(frozen=True, slots=True)
class TagTreeNode:
    """UI 树节点（service ↔ presenter 契约，不属于持久化模型）。"""

    tag: Tag
    children: tuple["TagTreeNode", ...] = ()
    depth: int = 0
    path: tuple[str, ...] = ()  # 从根到当前的 display_name 链（不含自身）

    @property
    def uuid(self) -> UUID:
        return self.tag.uuid

    @property
    def is_leaf(self) -> bool:
        return not self.children


@dataclass(frozen=True, slots=True)
class TagWithStats:
    tag: Tag
    asset_count: int  # 直接挂在该标签上的可见资产数
    child_count: int  # 直接子标签数
    subtree_asset_count: int  # 含子孙的资产数（仅 include_subtree=True 时计算）


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """restore 的结构化结果。"""

    tag: Optional[Tag]
    parent_visible: bool  # False = 已恢复但父仍软删，当前是孤儿


# ============================== Service ==============================


class TagService:
    """标签业务服务。

    孤儿策略（list_tree）：
        · "hide"（默认）：父链软删的子标签及其子树不进入返回结果；
        · "promote"：父链软删的子标签上提到根；
        · 用 list_orphans() 单独查询孤儿，供 UI 修复。

    restore 语义：
        · 不要求父链可见，但返回 RestoreResult.parent_visible=False
          告知调用方"已恢复但仍是孤儿"；
        · 唯一冲突 → TagRestoreConflict；
        · 已可见（幂等）→ 返回 tag，parent_visible=True；
        · 不存在 → TagNotFound。

    delete 语义：
        · cascade=False（默认）：有子标签时抛 TagHasChildren；
        · cascade=True：先软删所有子标签（仅直接子，不递归——
          list_tags 是扁平的，递归需要 service 自己展开，见下）；
        · 幂等：已软删的标签 get_tag 返回 None → TagNotFound。
    """

    def __init__(self, repo: "TagRepository"):
        self._repo = repo

    # ---------------------------------------------------------------- #
    # 读
    # ---------------------------------------------------------------- #

    def get(self, uuid: _UUIDLike) -> Tag:
        tag = self._repo.get_tag(uuid)
        if tag is None:
            raise TagNotFound(str(uuid))
        return tag

    def list_all(self) -> list[Tag]:
        return self._repo.list_tags()

    def list_with_stats(
            self, *, include_subtree: bool = False
    ) -> list[TagWithStats]:
        """全量可见标签 + 统计。一次 count_assets_bulk + 内存分组，无 N+1。"""
        tags = self._repo.list_tags()
        if not tags:
            return []

        # repo 返回 {canonical uuid str: count}（UuidKeyed），用 str(t.uuid) 取值
        counts = self._repo.count_assets_bulk([t.uuid for t in tags])

        children_map: dict[UUID, list[Tag]] = defaultdict(list)
        for t in tags:
            if t.parent_uuid is not None:
                children_map[t.parent_uuid].append(t)

        result: list[TagWithStats] = []
        for t in tags:
            own = counts.get(str(t.uuid), 0)
            child_count = len(children_map.get(t.uuid, ()))
            subtree = (
                self._subtree_asset_count(t.uuid, children_map, counts)
                if include_subtree else 0
            )
            result.append(TagWithStats(
                tag=t,
                asset_count=own,
                child_count=child_count,
                subtree_asset_count=subtree,
            ))
        return result

    def list_tree(
            self,
            *,
            orphan_policy: Literal["hide", "promote"] = "hide",
    ) -> list[TagTreeNode]:
        """构建可见树。

        父链在 repo 层如实返回（含软删父的 uuid），本方法根据
        orphan_policy 决定这些子树是否展示，把决策收口在 service。
        """
        if orphan_policy not in ("hide", "promote"):
            raise ValueError(f"unsupported orphan_policy: {orphan_policy!r}")

        tags = self._repo.list_tags()
        if not tags:
            return []

        by_uuid = {t.uuid: t for t in tags}
        children_map: dict[Optional[UUID], list[Tag]] = defaultdict(list)

        for t in tags:
            parent = t.parent_uuid
            if parent is not None and parent not in by_uuid:
                if orphan_policy == "hide":
                    continue
                parent = None  # promote
            children_map[parent].append(t)

        def _make(tag: Tag, depth: int, path: tuple[str, ...]) -> TagTreeNode:
            label = tag.display_name or tag.name
            kids = tuple(
                _make(k, depth + 1, path + (label,))
                for k in children_map.get(tag.uuid, ())
            )
            return TagTreeNode(tag=tag, children=kids, depth=depth, path=path)

        # children_map[None] 已按 repo 的 (sort_order, id) 顺序排好
        return [_make(r, 0, ()) for r in children_map.get(None, ())]

    def list_orphans(self) -> list[Tag]:
        """可见标签中「父链指向软删标签」的那些，供 UI 修复 / 提示。"""
        tags = self._repo.list_tags()
        visible = {t.uuid for t in tags}
        return [
            t for t in tags
            if t.parent_uuid is not None and t.parent_uuid not in visible
        ]

    def ancestors(self, tag_uuid: _UUIDLike) -> list[Tag]:
        """从根到父的祖先链（不含自身）。父链上有软删 / 脏环就截断。"""
        tag = self._repo.get_tag(tag_uuid)
        if tag is None:
            raise TagNotFound(str(tag_uuid))

        by_uuid = {t.uuid: t for t in self._repo.list_tags()}
        chain: list[Tag] = []
        seen: set[UUID] = set()
        cur = tag.parent_uuid
        while cur is not None and cur in by_uuid and cur not in seen:
            seen.add(cur)
            ancestor = by_uuid[cur]
            chain.append(ancestor)
            cur = ancestor.parent_uuid
        return list(reversed(chain))

    def breadcrumb(self, tag_uuid: _UUIDLike) -> list[Tag]:
        """祖先 + 自身，方便 UI 直接渲染面包屑。"""
        return self.ancestors(tag_uuid) + [self.get(tag_uuid)]

    # ---------------------------------------------------------------- #
    # 写
    # ---------------------------------------------------------------- #

    def create(
            self,
            name: str,
            parent_uuid: Optional[_UUIDLike] = None,
            *,
            display_name: Optional[str] = None,
            description: Optional[str] = None,
            sort_order: int = 0,
    ) -> UUID:
        name = name.strip()
        if not name:
            raise TagError("tag name is empty")

        parent = None if parent_uuid is None else to_uuid(parent_uuid)
        if parent is not None and not self._repo.exists(parent):
            raise TagParentNotFound(str(parent))

        existing = self._repo.find_by_name(parent, name)
        if existing is not None:
            raise TagNameConflict(name, existing)

        new_uuid = self._repo.create(TagCreate(
            name=name,
            display_name=display_name if display_name is not None else name,
            description=description,
            sort_order=sort_order,
            parent_uuid=parent,
        ))
        if new_uuid is None:
            # 并发兜底：查完到写之间有别人抢先建了同名标签
            raise TagNameConflict(name)
        return new_uuid

    def rename(self, tag_uuid: _UUIDLike, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise TagError("tag name is empty")

        tag = self._repo.get_tag(tag_uuid)
        if tag is None:
            raise TagNotFound(str(tag_uuid))

        existing = self._repo.find_by_name(tag.parent_uuid, new_name)
        if existing is not None and existing != tag.uuid:
            raise TagNameConflict(new_name, existing)

        if not self._repo.update(tag_uuid, TagUpdate(name=new_name)):
            raise TagNotFound(str(tag_uuid))

    def update(
            self,
            tag_uuid: _UUIDLike,
            *,
            display_name: Optional[str] = UNSET,
            description: Optional[str] = UNSET,
            sort_order: int = UNSET,
    ) -> None:
        """非名字字段的 PATCH。改名走 rename（要检查重名）。"""
        patch = TagUpdate(
            display_name=display_name,
            description=description,
            sort_order=sort_order,
        )
        if patch.is_empty():
            return
        if not self._repo.update(tag_uuid, patch):
            raise TagNotFound(str(tag_uuid))

    def move(
            self,
            tag_uuid: _UUIDLike,
            new_parent_uuid: Optional[_UUIDLike],
    ) -> None:
        tag = to_uuid(tag_uuid)
        new_parent = (
            None if new_parent_uuid is None else to_uuid(new_parent_uuid)
        )
        if new_parent == tag:
            raise TagCycle(str(tag), str(new_parent))

        if new_parent is not None and not self._repo.exists(new_parent):
            raise TagParentNotFound(str(new_parent))

        # 跨层环：沿新父向上追溯。list_tags 只给可见标签，
        # 所以父链软删就会截断——这已经是孤儿，不是环，无需报错。
        if new_parent is not None:
            parent_of = {t.uuid: t.parent_uuid for t in self._repo.list_tags()}
            cur: Optional[UUID] = new_parent
            seen: set[UUID] = set()
            while cur is not None:
                if cur == tag:
                    raise TagCycle(str(tag), str(new_parent))
                if cur in seen:
                    # 已有脏环，拒绝操作
                    raise TagCycle(str(tag), str(new_parent))
                seen.add(cur)
                cur = parent_of.get(cur)

        if not self._repo.move(tag, new_parent):
            # 已校验过 tag 存在 / 父存在 / 无环，False 只剩「目标层重名」
            raise TagMoveConflict(str(tag), str(new_parent))

    def delete(self, tag_uuid: _UUIDLike, *, cascade: bool = False) -> None:
        """软删单个标签。

        cascade=False（默认）：有直接子标签时抛 TagHasChildren；
        cascade=True：先软删所有直接子标签（扁平 list_tags 一次搞定），
                      再软删自身。
        """
        tag = self._repo.get_tag(tag_uuid)
        if tag is None:
            raise TagNotFound(str(tag_uuid))

        children = [
            t for t in self._repo.list_tags() if t.parent_uuid == tag.uuid
        ]
        if children and not cascade:
            raise TagHasChildren(
                str(tag.uuid), [str(c.uuid) for c in children]
            )

        for c in children:
            self._repo.soft_delete(c.uuid)
        self._repo.soft_delete(tag.uuid)

    def restore(self, tag_uuid: _UUIDLike) -> RestoreResult:
        if not self._repo.exists(tag_uuid, include_deleted=True):
            raise TagNotFound(str(tag_uuid))

        restored = self._repo.restore(tag_uuid)
        if not restored:
            # 三种可能：已可见（幂等）/ 唯一冲突
            if self._repo.exists(tag_uuid, include_deleted=False):
                return RestoreResult(
                    tag=self._repo.get_tag(tag_uuid),
                    parent_visible=True,
                )
            raise TagRestoreConflict(str(tag_uuid))

        tag = self._repo.get_tag(tag_uuid)
        parent_visible = (
                tag is not None
                and (tag.parent_uuid is None
                     or self._repo.exists(tag.parent_uuid))
        )
        return RestoreResult(tag=tag, parent_visible=parent_visible)

    # ---------------------------------------------------------------- #
    # 内部
    # ---------------------------------------------------------------- #

    @staticmethod
    def _subtree_asset_count(
            root_uuid: UUID,
            children_map: dict[UUID, list[Tag]],
            counts: UuidKeyed[int],
    ) -> int:
        total = counts.get(str(root_uuid), 0)
        seen: set[UUID] = {root_uuid}
        stack: list[UUID] = [c.uuid for c in children_map.get(root_uuid, ())]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            total += counts.get(str(cur), 0)
            stack.extend(c.uuid for c in children_map.get(cur, ()))
        return total
