#
""""""

import json
from enum import Enum
from uuid import UUID
from typing import Any

__all__ = ["normal", "to_uuid", "to_json", "json_default"]


def normal(v, t):
    if v is None:
        v = t()
    return v


def to_uuid(value: Any) -> UUID:
    """把 str / UUID 统一为 ``UUID``；其他类型按 ``str`` 转换。

    DTO 边界唯一转换点：``from_row`` 负责 ``str→UUID``，
    写路径（``to_insert_params`` / repo 绑定）负责 ``UUID→str``。
    """
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def to_json(self) -> str:
    """转换为 JSON 字符串；``Enum`` 成员由 ``default=`` 兜底转 ``value``。"""
    return json.dumps(self.to_dict(), ensure_ascii=False, default=json_default)


def json_default(obj: Any) -> Any:
    """``json.dumps`` 的兜底序列化：``Enum`` 转 ``value``，其余类型报错。

    ``to_dict`` 按契约保留 ``Enum`` 原样；存储 / 传输需要 JSON 时，
    走 ``to_json``，由本函数兜底转成 ``value``。不把转换塞进 ``to_dict``，
    是为了让 ``to_dict`` 保持浅转换、不污染运行时视图。
    """
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )
