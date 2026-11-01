#
"""相簿域命令：``album.*``（相簿自身的生命周期；成员增删在 ``asset.*``）。

分工（容易被问"为什么不在一个命令里"）
------------------------------------
* ``album.*``   —— 主语是**一个相簿**：建 / 改名 / 设封面 / 排序 / 删 / 恢复；
* ``asset.*``   —— 主语是**一批资产**：加进相册 / 移出 / 移动 / 复制。

两边的可见性口径完全一致（``albums.is_deleted = 0`` + 资产可见性），因为它们
背后是同一套 ``repositories/utils/visibility.py`` 条件。

线程语义：``AlbumService`` 只写 DB，不碰属主线程状态，不需要回主线程。
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container
from albuswall.dto.album.command import AlbumCreate, AlbumReorder, AlbumUpdate

from ._namespace import register_namespace
from ._params import as_bool, as_int, flag, opt, paging_params, pos

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


def _uuid_or_none(value: Any, *, field: str) -> Optional[UUID]:
    if value is None or value == "":
        return None
    return UUID(str(value)) if str(value) else None


def setup(registry: CommandRegistry, container: Container) -> None:
    albums = container.require("album_service")
    view = container.require("view_service")

    @registry.command(
        "album.list",
        help_="列出相簿（虚拟相簿在前，物理相簿在后）",
        params=(),
    )
    def album_list() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for uuid in view.list_album_uuids():
            album = view.get_album(uuid)
            if album is None:      # pragma: no cover - 并发删除
                continue
            row = _jsonable(album)
            row["virtual"] = view.get_scope(uuid) is not None
            row["scope"] = view.get_scope(uuid)
            rows.append(row)
        return rows

    @registry.command(
        "album.get",
        help_="查看单个相簿（不存在返回 null）",
        params=(pos("album_uuid", help="相簿 uuid"),),
    )
    def album_get(album_uuid: str) -> Optional[dict]:
        album = view.get_album(UUID(album_uuid))
        if album is None:
            return None
        row = _jsonable(album)
        row["virtual"] = view.get_scope(album_uuid) is not None
        return row

    @registry.command(
        "album.assets",
        help_="列出相簿内资产（虚拟相簿忽略分页）",
        params=(
            pos("album_uuid", help="相簿 uuid"),
            *paging_params(default=50),
        ),
    )
    def album_assets(
            album_uuid: str,
            limit: int = 50,
            offset: int = 0,
    ) -> list[dict[str, Any]]:
        assets = view.list_assets(
            UUID(album_uuid),
            offset=as_int(offset, field="offset", minimum=0),
            limit=as_int(limit, field="limit", minimum=1, maximum=500),
        )
        return [_jsonable(a) for a in assets]

    @registry.command(
        "album.create",
        help_="新建相簿（uuid 由服务生成），返回相簿 DTO",
        params=(
            pos("title", help="相簿名（空串会被拒绝）"),
            opt("description", default=None, help="描述"),
            opt("cover", default=None, help="封面资产 uuid（可省）"),
            opt("sort_order", type=int, default=0, help="排序权重"),
        ),
    )
    def album_create(
            title: str,
            description: Any = None,
            cover: Any = None,
            sort_order: int = 0,
    ) -> dict[str, Any]:
        created = albums.create(AlbumCreate(
            title=title,
            description=str(description or ""),
            cover_uuid=_uuid_or_none(cover, field="cover"),
            sort_order=as_int(sort_order or 0, field="sort_order"),
        ))
        return _jsonable(created)

    @registry.command(
        "album.rename",
        help_="给相簿改名",
        params=(
            pos("album_uuid", help="相簿 uuid"),
            pos("title", help="新名字"),
        ),
    )
    def album_rename(album_uuid: str, title: str) -> dict[str, Any]:
        return {
            "uuid": album_uuid,
            "changed": albums.rename(UUID(album_uuid), title),
        }

    @registry.command(
        "album.update",
        help_="局部更新相簿（只改传入的字段；--clear-cover 清空封面）",
        params=(
            pos("album_uuid", help="相簿 uuid"),
            opt("title", default=None, help="新名字"),
            opt("description", default=None, help="新描述"),
            opt("sort_order", type=int, default=None, help="新排序权重"),
            opt("album_type", type=int, default=None, help="相簿类型"),
            opt("cover", default=None, help="封面资产 uuid"),
            flag("clear_cover", help="清空封面（与不传 --cover 区别开）"),
        ),
    )
    def album_update(
            album_uuid: str,
            title: Any = None,
            description: Any = None,
            sort_order: Any = None,
            album_type: Any = None,
            cover: Any = None,
            clear_cover: bool = False,
    ) -> dict[str, Any]:
        """三态封面：不传 = 不动，``--cover`` = 设为该资产，``--clear-cover`` = 清空。"""
        patch: dict[str, Any] = {}
        fields: list[str] = []
        if title is not None:
            patch["title"] = title
            fields.append("title")
        if description is not None:
            patch["description"] = str(description)
            fields.append("description")
        if sort_order is not None:
            patch["sort_order"] = as_int(sort_order, field="sort_order")
            fields.append("sort_order")
        if album_type is not None:
            patch["album_type"] = as_int(
                album_type, field="album_type", minimum=0)
            fields.append("album_type")
        if as_bool(clear_cover, field="clear_cover"):
            patch["cover_uuid"] = None
            fields.append("cover")
        elif cover is not None:
            patch["cover_uuid"] = _uuid_or_none(cover, field="cover")
            fields.append("cover")
        if not patch:
            raise BadRequest("album.update: nothing to update")
        changed = albums.update(UUID(album_uuid), AlbumUpdate(**patch))
        return {"uuid": album_uuid, "changed": changed, "fields": sorted(fields)}

    @registry.command(
        "album.cover",
        help_="设置相簿封面（--clear 清空）",
        params=(
            pos("album_uuid", help="相簿 uuid"),
            opt("cover", default=None, help="封面资产 uuid"),
            flag("clear", help="清空封面"),
        ),
    )
    def album_cover(
            album_uuid: str, cover: Any = None, clear: bool = False,
    ) -> dict[str, Any]:
        cover_uuid = (
            None if as_bool(clear, field="clear")
            else _uuid_or_none(cover, field="cover")
        )
        changed = albums.set_cover(UUID(album_uuid), cover_uuid)
        return {
            "uuid": album_uuid,
            "cover": None if cover_uuid is None else str(cover_uuid),
            "changed": changed,
        }

    @registry.command(
        "album.reorder",
        help_="按给定顺序重排相簿（顺位即 sort_order）",
        params=(pos("album_uuids", help="相簿 uuid 列表，顺序即新排序"),),
    )
    def album_reorder(album_uuids: Any) -> dict[str, Any]:
        if isinstance(album_uuids, (str, UUID)):
            ordered = [str(album_uuids)]
        elif isinstance(album_uuids, (list, tuple)):
            ordered = [str(u) for u in album_uuids]
        else:
            raise BadRequest("album_uuids: expected a list of uuids")
        updated = albums.reorder(AlbumReorder(ordered))
        return {"updated": updated, "order": ordered}

    @registry.command(
        "album.delete",
        help_="软删相簿（成员关系默认保留；--cascade 一并清掉）",
        params=(
            pos("album_uuid", help="相簿 uuid"),
            flag("cascade", help="同时删除相簿内成员关系"),
        ),
    )
    def album_delete(album_uuid: str, cascade: bool = False) -> dict[str, Any]:
        deleted = albums.delete(
            UUID(album_uuid), cascade=as_bool(cascade, field="cascade"))
        return {"uuid": album_uuid, "deleted": deleted}

    @registry.command(
        "album.restore",
        help_="恢复被软删的相簿",
        params=(pos("album_uuid", help="相簿 uuid"),),
    )
    def album_restore(album_uuid: str) -> dict[str, Any]:
        return {
            "uuid": album_uuid,
            "restored": albums.restore(UUID(album_uuid)),
        }

    @registry.command(
        "album.count",
        help_="相簿成员数（不过滤资产可见性）",
        params=(pos("album_uuid", help="相簿 uuid"),),
    )
    def album_count(album_uuid: str) -> dict[str, Any]:
        return {
            "uuid": album_uuid,
            "members": albums.count_members(UUID(album_uuid)),
        }

    # ---- 命名空间入口（与 ``task`` 同一形状）----
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "album",
        help_="相簿命令入口：相簿自身的生命周期（album <sub> ...）",
        hint="成员增删在 asset 域：albuswall asset add-to-album <album> <uuids>",
    )
