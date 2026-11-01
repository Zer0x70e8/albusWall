#
"""DTO → JSON-safe dict 投影。

命令层的返回值要经 IPC（JSON 线路）回到 CLI / GUI，而 DTO 的
``to_dict()`` 只是 ``asdict``：``UUID`` / ``Enum`` 原样保留，直接喂
``json.dumps`` 会 ``TypeError``。本模块提供唯一投影口径：

* ``UUID``          → ``str``
* ``Enum``          → ``.value``
* ``datetime``/``date``/``time`` → ``isoformat()``
* 嵌套 dataclass    → 递归投影为 dict
* ``dict`` / ``list`` / ``tuple`` / ``set`` → 递归投影（set 转 list）
* 基本类型（str/int/float/bool/None）→ 原样返回

本文件不 import 任何 service，只依赖标准库 + DTO 类型标注
（``TYPE_CHECKING`` 下），保持"工具纯函数、无副作用、无状态"。

列表场景由调用方用 comprehension 组合，本模块不提供 ``*_list`` 版本。
"""

from __future__ import annotations

import datetime
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from albuswall.dto.album import Album, Asset
    from albuswall.dto.source.query import IngestSource
    from albuswall.dto.tag.query import Tag
    from albuswall.dto.thumbnail.query import MissingThumbnailRow
    from albuswall.dto.trash import TrashedAsset
    from albuswall.dto.trigger import TriggerConfig

__all__ = [
    "album_to_dict",
    "asset_to_dict",
    "missing_thumbnail_row_to_dict",
    "source_to_dict",
    "tag_to_dict",
    "trashed_asset_to_dict",
    "trigger_config_to_dict",
]


def _jsonable(value: Any) -> Any:
    """把任意 DTO 字段值递归投影为 JSON-safe 值。

    范围声明：只保证 ``UUID`` / ``Enum`` / ``datetime`` 族 / dataclass /
    容器被处理；契约外的标量（如自定义对象）原样透传，**本函数不是通用
    序列化器**，这类输入仍会让 ``json.dumps`` 抛 ``TypeError``（有意为之：
    让契约违例尽早暴露）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    # 契约外标量：原样返回，让 json.dumps 暴露而不是静默吞掉。
    return value


def _dto_to_dict(dto: Any) -> dict[str, Any]:
    """``_jsonable`` 的 dict 收口版本：保证返回 dict（dataclass 或 mapping）。"""
    projected = _jsonable(dto)
    if not isinstance(projected, dict):
        raise TypeError(
            f"expected a dataclass/mapping DTO, got {type(dto).__name__}"
        )
    return projected


def asset_to_dict(asset: Asset) -> dict[str, Any]:
    """``Asset`` DTO → JSON-safe dict（``uuid`` 转 str）。"""
    return _dto_to_dict(asset)


def album_to_dict(album: Album) -> dict[str, Any]:
    """``Album`` DTO → JSON-safe dict（``uuid`` / ``cover`` 转 str）。"""
    return _dto_to_dict(album)


def tag_to_dict(tag: Tag) -> dict[str, Any]:
    """``Tag`` DTO → JSON-safe dict（``uuid`` / ``parent_uuid`` 转 str）。"""
    return _dto_to_dict(tag)


def source_to_dict(source: IngestSource) -> dict[str, Any]:
    """``IngestSource`` DTO → JSON-safe dict（``file_type_check`` 枚举转 value）。"""
    return _dto_to_dict(source)


def trashed_asset_to_dict(ta: TrashedAsset) -> dict[str, Any]:
    """``TrashedAsset`` DTO → JSON-safe dict（``uuid`` 转 str）。"""
    return _dto_to_dict(ta)


def trigger_config_to_dict(cfg: TriggerConfig) -> dict[str, Any]:
    """``TriggerConfig`` DTO → JSON-safe dict（``update_mode`` 枚举转 value）。"""
    return _dto_to_dict(cfg)


def missing_thumbnail_row_to_dict(row: MissingThumbnailRow) -> dict[str, Any]:
    """``MissingThumbnailRow`` → JSON-safe dict。

    ``uuid`` 转 str；嵌套的 ``paths``（``ThumbnailPaths``）由 ``_jsonable``
    递归投影成 4 个路径字段。
    """
    return _dto_to_dict(row)
