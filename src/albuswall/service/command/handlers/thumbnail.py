#
"""缩略图命令：``thumbnail.*``（按需生成 + 维护）。

先说清"自动化程度"，因为这决定 UI 该调什么
------------------------------------------
缩略图**不再**在导入后批量回填。唯一自动入口是 ``ViewService.assets_viewed``
→ ``request_bulk``（用户看到哪页就排哪页）。所以：

* UI 正常拿图：读列表 → 装配层自动请求 → ``thumbnail_ready`` 广播后重读；
  一般**不需要**显式调用本模块的命令；
* 本模块的命令是给**运维 / 调试 / 特殊场景**用的：查统计、查路径、手动催
  一轮、扫孤儿。

长任务提醒
----------
``thumbnail.scan`` / ``thumbnail.run-once`` / ``thumbnail.clean`` 是同步的，
其中 ``run-once`` 会真的渲染图片、``clean`` 会遍历整个缓存目录。三者都
``hidden=True``（不是日常操作），并且都在 IPC 连接线程上执行 —— 大图库上
可能超过客户端超时。
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any
from uuid import UUID

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container

from ._namespace import register_namespace
from ._params import as_int, flag, opt, pos

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


def _uuid_list(value: Any, *, field: str) -> list[str]:
    """``uuid`` / ``"a,b"`` / ``["a", "b"]`` → 字符串列表（逗号串也收）。"""
    if value is None:
        return []
    if isinstance(value, UUID):
        return [str(value)]
    if isinstance(value, str):
        return [
            part.strip()
            for part in value.strip().strip("[]").split(",") if part.strip()
        ]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value]
    raise BadRequest(f"{field}: expected a uuid or a list of uuids")


def setup(registry: CommandRegistry, container: Container) -> None:
    thumbs = container.require("thumbnail_service")
    cleaner = container.require("thumbnail_cleaner")

    @registry.command(
        "thumbnail.stats",
        help_="缩略图生成统计（按状态计数）",
        params=(),
    )
    def thumbnail_stats() -> dict[str, Any]:
        return _jsonable(thumbs.stats())

    @registry.command(
        "thumbnail.describe",
        help_="缩略图服务自述（模式 / 根目录 / 在途任务）",
        params=(flag("verbose", help="附带依赖信息"),),
    )
    def thumbnail_describe(verbose: bool = False) -> dict[str, Any]:
        return {
            "service": thumbs.describe(verbose=bool(verbose)),
            "cleaner": cleaner.describe(verbose=bool(verbose)),
        }

    @registry.command(
        "thumbnail.paths",
        help_="批量查缩略图路径（未生成的不会出现在结果里）",
        params=(
            pos("asset_uuids", help="uuid 或 uuid 列表"),
            opt("spec", default="medium", help="small / medium / large"),
        ),
    )
    def thumbnail_paths(
            asset_uuids: Any, spec: str = "medium",
    ) -> dict[str, Any]:
        from albuswall.common.enums import ThumbSpec

        try:
            thumb_spec = ThumbSpec(str(spec).strip().lower())
        except ValueError:
            raise BadRequest(
                f"spec: {spec!r} not in "
                f"{tuple(s.value for s in ThumbSpec)!r}"
            ) from None
        uuids = _uuid_list(asset_uuids, field="asset_uuids")
        return {
            "spec": thumb_spec.value,
            "paths": thumbs.get_thumbnail_paths(uuids, thumb_spec),
        }

    @registry.command(
        "thumbnail.request",
        help_="按需请求生成（上限 batch_size；已在途/齐全/冷却期跳过）",
        params=(
            pos("asset_uuids", help="uuid 或 uuid 列表"),
            flag("deleted", help="包含回收站里的资产"),
        ),
    )
    def thumbnail_request(
            asset_uuids: Any, deleted: bool = False,
    ) -> dict[str, Any]:
        uuids = _uuid_list(asset_uuids, field="asset_uuids")
        submitted = thumbs.request_bulk(
            uuids, include_deleted=bool(deleted),
        )
        return {"requested": len(uuids), "submitted": submitted}

    @registry.command(
        "thumbnail.regenerate",
        help_="强制重建单张资产的缩略图（忽略缓存与冷却）",
        params=(pos("asset_uuid", help="资产 uuid"),),
    )
    def thumbnail_regenerate(asset_uuid: str) -> dict[str, Any]:
        future = thumbs.regenerate(str(asset_uuid))
        return {"uuid": str(asset_uuid), "accepted": future is not None}

    @registry.command(
        "thumbnail.scan",
        help_="把缺缩略图的活跃资产批量排进队列（每次最多 limit 个）",
        hidden=True,
        params=(opt("limit", type=int, default=None, help="本次最多提交数"),),
    )
    def thumbnail_scan(limit: Any = None) -> dict[str, Any]:
        batch = None if limit is None else as_int(
            limit, field="limit", minimum=1)
        return {"submitted": thumbs.scan_and_submit(batch)}

    @registry.command(
        "thumbnail.run-once",
        help_="同步跑一轮补齐（渲染并等结果；大图库会很慢）",
        hidden=True,
        params=(opt("limit", type=int, default=None, help="本轮最多处理数"),),
    )
    def thumbnail_run_once(limit: Any = None) -> dict[str, Any]:
        batch = None if limit is None else as_int(
            limit, field="limit", minimum=1)
        return _jsonable(thumbs.run_once(batch))

    @registry.command(
        "thumbnail.clean",
        help_="扫描并清理孤儿 PNG（源文件已不存在；按 PNG 内嵌 Thumb::URI 判定）",
        hidden=True,
        params=(flag("dry_run", help="只扫描不删除"),),
    )
    def thumbnail_clean(dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return _jsonable(cleaner.scan())
        return _jsonable(cleaner.scan_and_purge())

    @registry.command(
        "thumbnail.purge",
        help_="清掉若干资产的缩略图 DB 记录（只清库，文件交给 cleaner）",
        hidden=True,
        params=(
            pos("asset_uuids", help="uuid 或 uuid 列表"),
            flag("single", help="按单张逐条调用 purge"),
        ),
    )
    def thumbnail_purge(asset_uuids: Any, single: bool = False) -> dict[str, Any]:
        uuids = _uuid_list(asset_uuids, field="asset_uuids")
        if single:
            handled = sum(1 for u in uuids if thumbs.purge(u))
        else:
            handled = thumbs.purge_bulk(uuids)
        return {"requested": len(uuids), "cleared": handled}

    # ---- 命名空间入口（与 ``task`` 同一形状）----
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "thumbnail",
        help_="缩略图命令入口：按需生成与维护（thumbnail <sub> ...）",
        hint="正常看图的缩略图由 view 访问自动排队，一般不需要手动调这里。",
    )
