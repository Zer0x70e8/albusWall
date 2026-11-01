#
"""标签别名的用例编排：路径解析的"软链接"（``tag_aliases`` 的唯一写入口）。

语义（照抄 schema，代码只是把它落成不变量）
-------------------------------------------
* 别名有自己的**路径段名**（如 ``游戏A.link``），只用于**路径解析**；
* 解析路径命中别名时跳到 ``target_uuid`` 指向的**规范标签**；
* 别名**不参与**资产绑定 —— ``asset_tags.tag_uuid`` 必须指向 ``tags.uuid``；
* 应用层要保证：**禁环**（A → B → A）、尽量直接指向规范标签、
  别名不建议有子节点。

本模块负责其中可机械校验的两条：

1. **不指向别名**：``create`` 只接受规范标签作为目标 —— 仓储用
   ``JOIN tags`` 保证目标行在 ``tags`` 里，别名因此**结构上**无法指向别名，
   ``A → B → A`` 这种真环建不出来。
2. **不向上短路**：拒绝"目标是自己所在层或其祖先"的别名。这类别名不是环，
   但会让任何递归展开路径的调用方在一个子树里反复回到祖先 —— 语义上更像
   把子树接到自己下面，属于数据错误而不是别名该表达的东西
   （要合并子树请直接改 ``parent_id``）。

"别名不建议有子节点"只做**提示**不做强制：结构化上别名没有 ``tags.id``，
子节点只能挂在规范标签下，所以这一条天然成立（``parent_uuid`` 指向的是
``tags.uuid``，别名 uuid 当父只可能来自脏数据 —— 解析时当作解析不到）。

为什么不并进 ``TagService``：``TagService`` 的每个方法都在处理"标签自身的
生命周期"，而别名是**路径解析**的旁路设施；放一起会让那个类的方法表变成
两套不变量混在一起。两者只共享仓储。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence
from uuid import UUID

from albuswall.dto.tag.query import TagAlias
from albuswall.dto.utils import normalize_tag_name, to_uuid
from albuswall.log import Logger, getLogger

from .errors import TagError, TagNotFound, TagParentNotFound

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.repositories.tag import TagRepository

__all__ = ["AliasService", "AliasCycle"]

_UUIDLike = UUID | str

_logger: Logger = getLogger(__name__)

#: 约定后缀：路径段以它结尾一般表示"这是别名"。**只是约定**，解析时不强制
#: （历史数据可能没有后缀），但创建时若不带后缀会记一条 debug 日志。
ALIAS_SUFFIX = ".link"


class AliasCycle(TagError):
    """别名链成环（A → B → A），继续解析会死循环。

    与 ``TagCycle``（标签父链成环）不同：那是树结构问题，这是解析跳转问题。
    """

    def __init__(self, alias_uuid: str, target_uuid: str) -> None:
        super().__init__(
            f"alias cycle: {alias_uuid} -> {target_uuid}"
        )
        self.alias_uuid = alias_uuid
        self.target_uuid = target_uuid


class AliasService:
    """别名编排（被动服务：无后台行为、无信号）。

    线程语义：纯 DB 读写，不做线程切换；调用方（UI 写路径 / 命令层）自己
    决定在哪个线程调用。
    """

    def __init__(self, repo: "TagRepository") -> None:
        self._repo = repo

    def describe(self, *, verbose: bool = False) -> str:
        base = "AliasService()"
        if verbose:
            base = base[:-1] + f"repo={type(self._repo).__name__})"
        return base

    # ---------------------------------------------------------------- #
    # 读
    # ---------------------------------------------------------------- #
    def list_all(self) -> list[TagAlias]:
        """全部别名（按 id 升序）。"""
        return self._repo.list_aliases()

    def list_of_parent(
            self, parent_uuid: Optional[_UUIDLike] = None,
    ) -> list[TagAlias]:
        """某层下的别名；``parent_uuid=None`` 表示根层。"""
        if parent_uuid is None:
            return self._repo.list_root_aliases()
        return self._repo.list_aliases(parent_uuid)

    def get(self, alias_uuid: _UUIDLike) -> TagAlias:
        alias = self._repo.get_alias(alias_uuid)
        if alias is None:
            raise TagNotFound(str(alias_uuid))
        return alias

    def resolve(self, alias_uuid: _UUIDLike) -> UUID:
        """把别名解析成规范标签 uuid。

        仓储层的 ``JOIN tags AS tgt`` 已保证目标存在于 ``tags``，所以这里
        只有一步；保留这个方法是为了让调用方有一个**语义出口**，将来若要
        支持"别名指向别名"的链式解析，只改这里。
        """
        return self.get(alias_uuid).target_uuid

    # ---------------------------------------------------------------- #
    # 写
    # ---------------------------------------------------------------- #
    def create(
            self,
            name: str,
            target_uuid: _UUIDLike,
            parent_uuid: Optional[_UUIDLike] = None,
    ) -> UUID:
        """创建别名。

        Raises:
            TagError: 名字为空。
            TagParentNotFound: 父标签不存在 / 已软删。
            TagNotFound: 目标标签不存在 / 已软删，或目标本身是别名。
            AliasCycle: 目标是自己所在层或其祖先（向上短路，见
                :meth:`_guard_cycle`）。
            TagError: 同层重名（``TagNameConflict`` 的同族，见
                :mod:`albuswall.service.tag.errors`）。
        """
        name = name.strip()
        if not name:
            raise TagError("alias name is empty")
        if not name.endswith(ALIAS_SUFFIX):
            _logger.debug(
                "alias %r does not end with %r; allowed but non-idiomatic",
                name, ALIAS_SUFFIX,
            )

        parent = None if parent_uuid is None else to_uuid(parent_uuid)
        target = to_uuid(target_uuid)

        if parent is not None and not self._repo.exists(parent):
            raise TagParentNotFound(str(parent))
        if not self._repo.exists(target):
            raise TagNotFound(str(target))

        self._guard_cycle(target, parent)

        new_uuid = self._repo.create_alias(name, target, parent)
        if new_uuid is None:
            # 父/目标刚好被软删（并发），或同层重名
            from .errors import TagNameConflict

            existing = self._repo.find_alias_by_name(parent, name)
            raise TagNameConflict(
                name, existing.uuid if existing is not None else None,
            )
        return new_uuid

    def delete(self, alias_uuid: _UUIDLike) -> bool:
        """删别名；返回是否真的删掉（不存在返回 False，幂等）。"""
        return self._repo.delete_alias(alias_uuid)

    # ---------------------------------------------------------------- #
    # 内部
    # ---------------------------------------------------------------- #
    def _guard_cycle(
            self,
            target_uuid: UUID,
            parent_uuid: Optional[UUID],
    ) -> None:
        """拒绝"向上短路"的别名：目标必须是**旁支**，不能是自己所在层或祖先。

        判定口径：从 ``alias 的父层`` 沿父链向上走，若撞见 ``target_uuid``
        说明目标是自己这一支的祖先 —— 那么 ``parent/别名`` 会解析回自己的
        祖先，任何递归展开都会在同一子树里来回跳。这不是环（跳转一次就停），
        但把子树接到自己下面是数据错误，应当当场拒绝。

        父链数据只取**可见**标签（``list_tags``）：父链软删就会截断 —— 那已经
        是孤儿而不是结构问题，不必报错。

        真正的 ``A → B → A`` 环在这里建不出来：别名目标只能是 ``tags`` 行
        （仓储的 ``JOIN tags`` 保证），而 ``tags`` 的父链是严格树。
        """
        if parent_uuid is None:
            return

        parent_of = {t.uuid: t.parent_uuid for t in self._repo.list_tags()}
        cur: Optional[UUID] = parent_uuid
        seen: set[UUID] = set()
        while cur is not None:
            if cur == target_uuid:
                raise AliasCycle(str(parent_uuid), str(target_uuid))
            if cur in seen:
                # 已有脏环：拒绝新建，避免把脏数据继续放大
                raise AliasCycle(str(parent_uuid), str(target_uuid))
            seen.add(cur)
            cur = parent_of.get(cur)


def resolve_alias_in_path(
        aliases: Sequence[TagAlias],
        segment: str,
) -> Optional[UUID]:
    """在给定层的别名清单里按路径段名查目标标签 uuid。

    纯函数：调用方自己决定要不要为每一层预取 ``aliases``（一次树遍历只要
    一次 ``list_aliases()`` 就能建好 ``(parent_uuid, norm_name) → target``
    的索引）。段名比较用 :func:`normalize_tag_name`，与 ``tags.norm_name``
    同一套归一化，避免大小写 / Unicode 差异导致的漏命中。
    """
    wanted = normalize_tag_name(segment)
    for alias in aliases:
        if alias.norm_name == wanted:
            return alias.target_uuid
    return None
