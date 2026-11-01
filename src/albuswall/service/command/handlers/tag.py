#
"""标签域命令：``tag.*``（标签树 + 别名）。

两块的分工与 ``service/tag/`` 一致：

* ``tag.*``        —— 标签自身：树 / 建删改移 / 路径 / 搜索 / 软删恢复；
* ``tag.alias-*``  —— 路径解析的"软链接"（不参与资产绑定）。名字用连字符
  而不是 ``tag.alias.list`` 这样的多段路径，是为了让 ``albuswall tag
  alias-list`` 这种两段式调用直接可用（见 ``_namespace.py``）。

``asset.tags`` / ``asset.tag-add`` 那些"资产挂了哪些标签"在 ``asset.*`` 域。

线程语义：只写 ``tags`` / ``tag_aliases``，不碰属主线程状态，无需回主线程。
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container

from ._namespace import register_namespace
from ._params import as_bool, as_int, opt, paging_params, pos

__all__: list[str] = []


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _opt_uuid(value: Any) -> Optional[UUID]:
    text = str(value or "").strip()
    return UUID(text) if text else None


def setup(registry: CommandRegistry, container: Container) -> None:
    tags = container.require("tag_service")
    aliases = container.require("alias_service")
    tagging = container.require("asset_tag_service")

    # ---------------- 读 ----------------
    @registry.command(
        "tag.list",
        help_="标签树（--flat 只列扁平清单，--orphan promote/hide 处理孤儿）",
        params=(
            opt("flat", type=bool, default=False, help="扁平清单而不是树"),
            opt("orphan", default="hide", help="孤儿策略：hide / promote"),
        ),
    )
    def tag_list(flat: Any = False, orphan: str = "hide") -> list[dict[str, Any]]:
        if as_bool(flat, field="flat"):
            return [_jsonable(t) for t in tags.list_all()]
        policy = str(orphan or "hide").strip().lower()
        if policy not in ("hide", "promote"):
            raise BadRequest(f"orphan: {policy!r} not in ('hide', 'promote')")
        return [_jsonable(node) for node in tags.list_tree(orphan_policy=policy)]

    @registry.command(
        "tag.stats",
        help_="标签 + 资产数（--subtree 一并统计整棵子树）",
        params=(
            opt("subtree", type=bool, default=False, help="统计子树资产数"),
        ),
    )
    def tag_stats(subtree: Any = False) -> list[dict[str, Any]]:
        return [
            _jsonable(row) for row in tags.list_with_stats(
                include_subtree=as_bool(subtree, field="subtree"))
        ]

    @registry.command(
        "tag.get",
        help_="查看单个标签（含祖先链与展示路径）",
        params=(pos("tag_uuid", help="标签 uuid"),),
    )
    def tag_get(tag_uuid: str) -> dict[str, Any]:
        uuid = UUID(tag_uuid)
        return {
            "tag": _jsonable(tags.get(uuid)),
            "ancestors": [_jsonable(t) for t in tags.ancestors(uuid)],
            "path": tags.path_label(uuid),
        }

    @registry.command(
        "tag.search",
        help_="按名称模糊搜索标签（带上作用域内资产数）",
        params=(
            pos("keyword", help="关键词"),
            opt("album", default=None, help="只统计该相簿内的资产"),
            opt("scope", default="active", help="active / deleted"),
            *paging_params(default=20),
        ),
    )
    def tag_search(
            keyword: str,
            album: Any = None,
            scope: str = "active",
            limit: int = 20,
            offset: int = 0,
    ) -> list[dict[str, Any]]:
        # 仓储的 search 只接受 limit（不分页），offset 仅为与本域其它命令
        # 保持一致的参数形状而存在。
        del offset
        album_uuid = _opt_uuid(album)
        return [
            _jsonable(m) for m in tags.search(
                keyword,
                album_uuid=album_uuid,
                scope=str(scope or "active"),
                limit=as_int(limit, field="limit", minimum=1, maximum=200),
            )
        ]

    @registry.command(
        "tag.assets",
        help_="标签下的资产 uuid（默认含整棵子树）",
        params=(
            pos("tag_uuid", help="标签 uuid"),
            opt("scope", default="active", help="active / deleted"),
            opt("no_descendants", type=bool, default=False,
                help="只看该标签自己绑定的资产"),
        ),
    )
    def tag_assets(
            tag_uuid: str,
            scope: str = "active",
            no_descendants: Any = False,
    ) -> list[str]:
        return [
            str(u) for u in tags.asset_uuids(
                UUID(tag_uuid),
                scope=str(scope or "active"),
                include_descendants=not as_bool(
                    no_descendants, field="no_descendants"),
            )
        ]

    @registry.command(
        "tag.orphans",
        help_="父链指向软删标签的孤儿标签（供修复）",
        params=(),
    )
    def tag_orphans() -> list[dict[str, Any]]:
        return [_jsonable(t) for t in tags.list_orphans()]

    # ---------------- 写 ----------------
    @registry.command(
        "tag.create",
        help_="新建标签（同层重名会被拒绝）",
        params=(
            pos("name", help="标签名"),
            opt("parent", default=None, help="父标签 uuid"),
            opt("display_name", default=None, help="展示名（默认同 name）"),
            opt("description", default=None, help="备注"),
            opt("sort_order", type=int, default=0, help="排序权重"),
        ),
    )
    def tag_create(
            name: str,
            parent: Any = None,
            display_name: Any = None,
            description: Any = None,
            sort_order: int = 0,
    ) -> dict[str, Any]:
        created = tags.create(
            name,
            _opt_uuid(parent),
            display_name=None if display_name is None else str(display_name),
            description=None if description is None else str(description),
            sort_order=as_int(sort_order or 0, field="sort_order"),
        )
        return {"uuid": str(created), "name": name}

    @registry.command(
        "tag.ensure-path",
        help_="按 a/b/c 路径复用或创建标签链，返回最深标签 uuid",
        params=(pos("path", help="路径，如 旅行/日本/京都"),),
    )
    def tag_ensure_path(path: str) -> dict[str, Any]:
        return {"uuid": str(tags.ensure_path(path)), "path": path}

    @registry.command(
        "tag.rename",
        help_="给标签改名（同层重名会被拒绝）",
        params=(
            pos("tag_uuid", help="标签 uuid"),
            pos("name", help="新名字"),
        ),
    )
    def tag_rename(tag_uuid: str, name: str) -> dict[str, Any]:
        tags.rename(UUID(tag_uuid), name)
        return {"uuid": tag_uuid, "name": name.strip()}

    @registry.command(
        "tag.update",
        help_="更新展示名 / 备注 / 排序（改名用 tag.rename）",
        params=(
            pos("tag_uuid", help="标签 uuid"),
            opt("display_name", default=None, help="展示名"),
            opt("description", default=None, help="备注"),
            opt("sort_order", type=int, default=None, help="排序权重"),
        ),
    )
    def tag_update(
            tag_uuid: str,
            display_name: Any = None,
            description: Any = None,
            sort_order: Any = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if display_name is not None:
            kwargs["display_name"] = str(display_name)
        if description is not None:
            kwargs["description"] = str(description)
        if sort_order is not None:
            kwargs["sort_order"] = as_int(sort_order, field="sort_order")
        if not kwargs:
            raise BadRequest("tag.update: nothing to update")
        tags.update(UUID(tag_uuid), **kwargs)
        return {"uuid": tag_uuid, "fields": sorted(kwargs)}

    @registry.command(
        "tag.move",
        help_="把标签挂到新父下（--root 表示移到根；成环会被拒绝）",
        params=(
            pos("tag_uuid", help="标签 uuid"),
            opt("parent", default=None, help="新父标签 uuid"),
            opt("root", type=bool, default=False, help="移到根层"),
        ),
    )
    def tag_move(
            tag_uuid: str, parent: Any = None, root: Any = False,
    ) -> dict[str, Any]:
        new_parent = None if as_bool(root, field="root") else _opt_uuid(parent)
        tags.move(UUID(tag_uuid), new_parent)
        return {
            "uuid": tag_uuid,
            "parent": None if new_parent is None else str(new_parent),
        }

    @registry.command(
        "tag.delete",
        help_="软删标签（有子标签时需 --cascade）",
        params=(
            pos("tag_uuid", help="标签 uuid"),
            opt("cascade", type=bool, default=False,
                help="连同整棵子树一起软删"),
        ),
    )
    def tag_delete(tag_uuid: str, cascade: Any = False) -> dict[str, Any]:
        tags.delete(UUID(tag_uuid),
                    cascade=as_bool(cascade, field="cascade"))
        return {"uuid": tag_uuid, "deleted": True}

    @registry.command(
        "tag.restore",
        help_="恢复被软删的标签",
        params=(pos("tag_uuid", help="标签 uuid"),),
    )
    def tag_restore(tag_uuid: str) -> dict[str, Any]:
        result = tags.restore(UUID(tag_uuid))
        return {
            "tag": _jsonable(result.tag),
            "parent_visible": result.parent_visible,
        }

    @registry.command(
        "tag.count",
        help_="标签下的资产数（或批量：传多个 uuid）",
        params=(pos("tag_uuid", help="标签 uuid 或 uuid 列表"),),
    )
    def tag_count(tag_uuid: Any) -> dict[str, Any]:
        if isinstance(tag_uuid, (list, tuple)):
            uuids = [UUID(str(u)) for u in tag_uuid]
            return _jsonable(tagging.count_for_tags(uuids))
        uuid = UUID(str(tag_uuid))
        return {"uuid": str(uuid), "assets": tagging.count_for_tag(uuid)}

    # ---------------- 别名 ----------------
    @registry.command(
        "tag.alias-list",
        help_="列出别名（--parent 只看某层）",
        params=(opt("parent", default=None, help="父标签 uuid"),),
    )
    def alias_list(parent: Any = None) -> list[dict[str, Any]]:
        rows = aliases.list_of_parent(_opt_uuid(parent))
        return [_jsonable(a) for a in rows]

    @registry.command(
        "tag.alias-create",
        help_="建别名（指向规范标签；向上短路会被拒绝）",
        params=(
            pos("name", help="别名段名，约定以 .link 结尾"),
            pos("target_uuid", help="目标规范标签 uuid"),
            opt("parent", default=None, help="挂在哪个标签层下"),
        ),
    )
    def alias_create(
            name: str, target_uuid: str, parent: Any = None,
    ) -> dict[str, Any]:
        created = aliases.create(
            name, UUID(target_uuid), _opt_uuid(parent),
        )
        return {"uuid": str(created), "name": name, "target": target_uuid}

    @registry.command(
        "tag.alias-resolve",
        help_="把别名解析成规范标签 uuid",
        params=(pos("alias_uuid", help="别名 uuid"),),
    )
    def alias_resolve(alias_uuid: str) -> dict[str, Any]:
        return {
            "alias": alias_uuid,
            "target": str(aliases.resolve(UUID(alias_uuid))),
        }

    @registry.command(
        "tag.alias-delete",
        help_="删别名（别名没有软删语义，直接删行）",
        params=(pos("alias_uuid", help="别名 uuid"),),
    )
    def alias_delete(alias_uuid: str) -> dict[str, Any]:
        return {
            "uuid": alias_uuid,
            "deleted": aliases.delete(UUID(alias_uuid)),
        }

    # ---- 命名空间入口（与 ``task`` 同一形状）----
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "tag",
        help_="标签命令入口：标签树与别名（tag <sub> ...）",
        hint="别名相关是 tag alias-* 子命令：tag alias-list / alias-create …",
    )
