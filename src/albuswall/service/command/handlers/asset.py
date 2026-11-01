#
"""资产域命令：``asset.*``（读观测 + 写命令 + 标签绑定）。

线程语义（本层不做线程处理）
---------------------------
资产写用例（``AssetOpsService``）只写 DB、不碰任何"属主线程状态"，所以
handler 直接在 IPC 工作线程调用即可 —— 与 ``source.*`` 需要回主线程不同，
这里**不需要** ``MainLoopService``。

分层边界：本模块只 ``container.require`` 四个 **service**
（``view_service`` / ``asset_ops_service`` / ``asset_tag_service`` /
``thumbnail_service``），不碰仓储与 DTO 之外的基础设施
（``tests/test_handlers_contract.py`` 有 AST 断言）。

返回约定
--------
* 读命令输出 JSON-able dict（枚举转值、UUID 转字符串、dataclass 摊平）；
* 写命令回**实际发生了什么**（``changed`` / ``skipped`` / ``not_found``），
  不回声输入 —— 撤销栈与 UI 只认仓储真做成的那些。
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
from ._params import as_bool, as_int, as_uuid, flag, opt, paging_params, pos

__all__: list[str] = []


# ==================== 序列化 ====================

def _jsonable(value: Any) -> Any:
    """递归把服务层返回值摊成 JSON-able（UUID / Enum / dataclass）。"""
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


def _asset_rows(assets: list[Any]) -> list[dict[str, Any]]:
    """资产 DTO → 一屏能渲染的行（缩略图路径按 ``medium`` 给一份）。"""
    rows: list[dict[str, Any]] = []
    for asset in assets:
        row = {f.name: _jsonable(getattr(asset, f.name))
               for f in dataclasses.fields(asset)}
        rows.append(row)
    return rows


def _uuid_list(value: Any, *, field: str) -> list[UUID]:
    """``uuid`` / ``"a,b"`` / ``["a", "b"]`` → ``[UUID, ...]``。

    三种形态都收（与 ``source.create --file_types`` 的逗号约定一致）：
    CLI 侧 ``ast.literal_eval`` 拼不出列表时（``"a,b"`` 不是合法字面量）就
    原样传字符串，这里负责切开 —— 否则 ``albusctl asset.trash a,b`` 会报
    ``invalid uuid``，用户只能写 Python 字面量 ``"['a','b']"``。
    """
    if value is None:
        return []
    if isinstance(value, (str, UUID)):
        text = str(value).strip()
        if not text:
            return []
        if isinstance(value, UUID):
            return [value]
        return [as_uuid(part.strip(), field=field)
                for part in text.strip("[]").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [as_uuid(v, field=field) for v in value]
    raise BadRequest(f"{field}: expected a uuid or a list of uuids")


# ==================== 装配 ====================

def setup(registry: CommandRegistry, container: Container) -> None:
    view = container.require("view_service")
    ops = container.require("asset_ops_service")
    tagging = container.require("asset_tag_service")
    # 缩略图只用来「请求 / 查路径」，不参与写路径。
    thumbs = container.require("thumbnail_service")

    # ---------------- 读 ----------------
    @registry.command(
        "asset.list",
        help_="按相册列出资产（album 可给虚拟相册 uuid）",
        params=(
            pos("album_uuid", help="相簿 uuid（All / Trash 也可）"),
            *paging_params(default=50),
        ),
    )
    def asset_list(
            album_uuid: str,
            limit: int = 50,
            offset: int = 0,
    ) -> list[dict[str, Any]]:
        album = as_uuid(album_uuid, field="album_uuid")
        limit = as_int(limit, field="limit", minimum=1, maximum=500)
        offset = as_int(offset, field="offset", minimum=0)
        return _asset_rows(view.list_assets(album, offset=offset, limit=limit))

    @registry.command(
        "asset.get",
        help_="查看单张资产（不存在返回 null）",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            flag("deleted", help="包含回收站里的资产"),
        ),
    )
    def asset_get(asset_uuid: str, deleted: bool = False) -> Optional[dict]:
        uuid = as_uuid(asset_uuid, field="asset_uuid")
        asset = view.get_asset(uuid, include_deleted=as_bool(
            deleted, field="deleted"))
        return None if asset is None else _jsonable(asset)

    @registry.command(
        "asset.uuids",
        help_="按相册列出资产 uuid（只给导航用的轻量清单）",
        params=(pos("album_uuid", help="相簿 uuid"),),
    )
    def asset_uuids(album_uuid: str) -> list[str]:
        album = as_uuid(album_uuid, field="album_uuid")
        return [str(u) for u in view.get_asset_uuids(album)]

    @registry.command(
        "asset.path",
        help_="资产/相簿封面的磁盘完整路径",
        params=(
            pos("uuid", help="资产 uuid 或相簿 uuid"),
            flag("album", help="把 uuid 当作相簿，取封面路径"),
            flag("deleted", help="包含回收站里的资产"),
        ),
    )
    def asset_path(
            uuid: str, album: bool = False, deleted: bool = False,
    ) -> dict[str, Any]:
        target = as_uuid(uuid, field="uuid")
        include_deleted = as_bool(deleted, field="deleted")
        path = (
            view.get_cover_full_path(target, include_deleted=include_deleted)
            if as_bool(album, field="album")
            else view.get_asset_full_path(
                target, include_deleted=include_deleted)
        )
        return {"uuid": str(target), "path": path}

    @registry.command(
        "asset.classify",
        help_="把一批资产分到 visible / deleted / missing 三类",
        params=(pos("asset_uuids", help="uuid 或 uuid 列表"),),
    )
    def asset_classify(asset_uuids: Any) -> dict[str, list[str]]:
        classified = ops.classify_assets(
            _uuid_list(asset_uuids, field="asset_uuids"))
        return {
            "visible": [str(u) for u in classified.visible],
            "deleted": [str(u) for u in classified.deleted],
            "missing": [str(u) for u in classified.missing],
        }

    # ---------------- 写 ----------------
    @registry.command(
        "asset.favorite",
        help_="设置 / 取消收藏（值没变则不写库）",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            pos("value", type=bool, help="true=收藏，false=取消"),
        ),
    )
    def asset_favorite(asset_uuid: str, value: bool) -> dict[str, Any]:
        uuid = as_uuid(asset_uuid, field="asset_uuid")
        return _jsonable(ops.set_favorite(uuid, as_bool(value, field="value")))

    @registry.command(
        "asset.trash",
        help_="把一批资产移进回收站（软删，可恢复）",
        params=(pos("asset_uuids", help="uuid 或 uuid 列表"),),
    )
    def asset_trash(asset_uuids: Any) -> dict[str, Any]:
        return _jsonable(
            ops.trash(_uuid_list(asset_uuids, field="asset_uuids"))
        )

    @registry.command(
        "asset.restore",
        help_="从回收站恢复一批资产（路径冲突的会在 conflicts 里）",
        params=(pos("asset_uuids", help="uuid 或 uuid 列表"),),
    )
    def asset_restore(asset_uuids: Any) -> dict[str, Any]:
        return _jsonable(
            ops.restore(_uuid_list(asset_uuids, field="asset_uuids"))
        )

    @registry.command(
        "asset.add-to-album",
        help_="把一批资产加入相簿（只加不删）",
        params=(
            pos("album_uuid", help="目标相簿 uuid"),
            pos("asset_uuids", help="uuid 或 uuid 列表"),
        ),
    )
    def asset_add_to_album(album_uuid: str, asset_uuids: Any) -> dict[str, Any]:
        return _jsonable(ops.add_to_album(
            as_uuid(album_uuid, field="album_uuid"),
            _uuid_list(asset_uuids, field="asset_uuids"),
        ))

    @registry.command(
        "asset.remove-from-album",
        help_="把一批资产从相簿移除（只删不加）",
        params=(
            pos("album_uuid", help="目标相簿 uuid"),
            pos("asset_uuids", help="uuid 或 uuid 列表"),
        ),
    )
    def asset_remove_from_album(
            album_uuid: str, asset_uuids: Any,
    ) -> dict[str, Any]:
        return _jsonable(ops.remove_from_album(
            as_uuid(album_uuid, field="album_uuid"),
            _uuid_list(asset_uuids, field="asset_uuids"),
        ))

    @registry.command(
        "asset.move",
        help_="把一批资产从源相簿移动到目标相簿（单事务，失败整体回滚）",
        params=(
            pos("src_album_uuid", help="源相簿 uuid"),
            pos("dst_album_uuid", help="目标相簿 uuid"),
            pos("asset_uuids", help="uuid 或 uuid 列表"),
        ),
    )
    def asset_move(
            src_album_uuid: str,
            dst_album_uuid: str,
            asset_uuids: Any,
    ) -> dict[str, Any]:
        return _jsonable(ops.move_between_albums(
            as_uuid(src_album_uuid, field="src_album_uuid"),
            as_uuid(dst_album_uuid, field="dst_album_uuid"),
            _uuid_list(asset_uuids, field="asset_uuids"),
        ))

    @registry.command(
        "asset.copy-to-album",
        help_="把一批资产复制到相簿（等价于加入目标相簿）",
        params=(
            pos("album_uuid", help="目标相簿 uuid"),
            pos("asset_uuids", help="uuid 或 uuid 列表"),
        ),
    )
    def asset_copy(album_uuid: str, asset_uuids: Any) -> dict[str, Any]:
        return _jsonable(ops.copy_into_album(
            as_uuid(album_uuid, field="album_uuid"),
            _uuid_list(asset_uuids, field="asset_uuids"),
        ))

    # ---------------- 资产 × 标签 ----------------
    @registry.command(
        "asset.tags",
        help_="列出资产挂的标签",
        params=(pos("asset_uuid", help="资产 uuid"),),
    )
    def asset_tags(asset_uuid: str) -> list[dict[str, Any]]:
        uuid = as_uuid(asset_uuid, field="asset_uuid")
        return [_jsonable(t) for t in tagging.list_of_asset(uuid)]

    @registry.command(
        "asset.tag-add",
        help_="给资产打标签（不存在的标签会被跳过）",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            pos("tag_uuids", help="tag uuid 或 uuid 列表"),
        ),
    )
    def asset_tag_add(asset_uuid: str, tag_uuids: Any) -> dict[str, Any]:
        result = tagging.add(
            as_uuid(asset_uuid, field="asset_uuid"),
            _uuid_list(tag_uuids, field="tag_uuids"),
        )
        return _jsonable(result)

    @registry.command(
        "asset.tag-remove",
        help_="移除资产的标签绑定",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            pos("tag_uuids", help="tag uuid 或 uuid 列表"),
        ),
    )
    def asset_tag_remove(asset_uuid: str, tag_uuids: Any) -> dict[str, Any]:
        removed = tagging.remove(
            as_uuid(asset_uuid, field="asset_uuid"),
            _uuid_list(tag_uuids, field="tag_uuids"),
        )
        return {"removed": removed}

    @registry.command(
        "asset.tags-set",
        help_="全量设置资产的标签（按 diff 增删，幂等）",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            pos("tag_uuids", help="tag uuid 或 uuid 列表；空列表 = 全部清空"),
        ),
    )
    def asset_tags_set(asset_uuid: str, tag_uuids: Any) -> dict[str, Any]:
        result = tagging.set(
            as_uuid(asset_uuid, field="asset_uuid"),
            _uuid_list(tag_uuids, field="tag_uuids"),
        )
        return _jsonable(result)

    # ---------------- 缩略图（按需） ----------------
    @registry.command(
        "asset.thumbnail-request",
        help_="请求生成缩略图（按需；已在途 / 齐全 / 冷却期的不重复排）",
        params=(
            pos("asset_uuids", help="uuid 或 uuid 列表"),
            flag("deleted", help="包含回收站里的资产"),
        ),
    )
    def asset_thumbnail_request(
            asset_uuids: Any, deleted: bool = False,
    ) -> dict[str, Any]:
        uuids = _uuid_list(asset_uuids, field="asset_uuids")
        submitted = thumbs.request_bulk(
            uuids, include_deleted=as_bool(deleted, field="deleted"),
        )
        return {"requested": len(uuids), "submitted": submitted}

    @registry.command(
        "asset.thumbnail-path",
        help_="查资产缩略图的磁盘路径（未生成返回 null）",
        params=(
            pos("asset_uuid", help="资产 uuid"),
            opt("spec", default="medium",
                help="small / medium / large"),
        ),
    )
    def asset_thumbnail_path(asset_uuid: str, spec: str = "medium") -> dict:
        from albuswall.common.enums import ThumbSpec

        uuid = as_uuid(asset_uuid, field="asset_uuid")
        try:
            thumb_spec = ThumbSpec(str(spec).strip().lower())
        except ValueError:
            raise BadRequest(
                f"spec: {spec!r} not in "
                f"{tuple(s.value for s in ThumbSpec)!r}"
            ) from None
        return {
            "uuid": str(uuid),
            "spec": thumb_spec.value,
            "path": thumbs.get_thumbnail_path(str(uuid), thumb_spec),
        }

    # ---- 命名空间入口（与 ``task`` 同一形状）----
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "asset",
        help_="资产命令入口：资产写路径与只读观测（asset <sub> ...）",
        hint="写命令主语是「一批资产」：收藏 / 回收站 / 相册成员 / 标签绑定。",
    )
