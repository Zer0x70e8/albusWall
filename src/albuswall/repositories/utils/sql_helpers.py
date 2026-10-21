#
"""SQL 辅助工具函数，纯函数、无状态。

三条契约：

* ``placeholders``  —— 生成 ``?, ?, ?`` 占位符，空集合必须由调用方短路；
* ``chunked``       —— 按 ``IN (...)`` 参数上限切片，批量写读统一走它；
* ``as_uuid_str``   —— UUID 绑定到 SQL 参数前的唯一转换点（sqlite3
  没有默认 ``UUID`` adapter，直接绑定会抛 ``ProgrammingError``）。
"""

from __future__ import annotations

from typing import Final, Iterator, Sequence, TypeVar, Union
from uuid import UUID

__all__ = [
    "IN_CLAUSE_CHUNK",
    "as_uuid_str",
    "build_set_clause",
    "chunked",
    "filter_dict",
    "placeholders",
]

T = TypeVar("T")

_UUIDLike = Union[UUID, str]

IN_CLAUSE_CHUNK: Final[int] = 900
"""单条 ``IN (...)`` 的最大参数个数。

低于 SQLite 默认 ``SQLITE_MAX_VARIABLE_NUMBER = 999``，兼顾旧版上限，
同时给同一条语句的其它参数留出余量。
"""


def placeholders(count: int) -> str:
    if not isinstance(count, int):
        raise TypeError(f"count must be int, got {type(count).__name__}")
    if count <= 0:
        # 空 IN 子句在 SQL 里是非法的，调用方必须自己短路。
        # 这里主动抛错，避免生成 "IN ()" 这种脏 SQL。
        raise ValueError("placeholders(count) requires count >= 1")
    return ", ".join("?" for _ in range(count))


def chunked(
        values: Sequence[T],
        size: int = IN_CLAUSE_CHUNK,
) -> Iterator[Sequence[T]]:
    """把序列切成不超过 ``size`` 的片段，供 ``IN (...)`` 批量语句使用。

    空序列不产出任何片段，调用方无需自己判断；切片保留原序列类型。
    """
    if size <= 0:
        raise ValueError("chunked(size) requires size >= 1")
    for start in range(0, len(values), size):
        yield values[start:start + size]


def as_uuid_str(value: _UUIDLike) -> str:
    """写路径 / 绑定路径：``UUID`` → ``str``，``str`` 原样返回。

    契约：DTO 对外统一用 ``UUID``（见 ``dto/CONVENTIONS.md`` · UUID 统一），
    SQL 绑定只接受 ``str``；所有把 uuid 塞进 SQL 参数的地方都必须过本函数。
    """
    return value if isinstance(value, str) else str(value)


def filter_dict(data: dict, allowed_keys: set) -> dict:
    if not isinstance(data, dict):
        raise TypeError(f"data must be dict, got {type(data).__name__}")
    if not isinstance(allowed_keys, (set, frozenset)):
        raise TypeError(
            f"allowed_keys must be set/frozenset, got {type(allowed_keys).__name__}"
        )
    return {k: v for k, v in data.items() if k in allowed_keys}


def build_set_clause(updates: dict):
    if not isinstance(updates, dict):
        raise TypeError(f"updates must be dict, got {type(updates).__name__}")
    if not updates:
        # 返回空串 + 空 list，让调用方决定是短路还是报错；
        # 比抛异常更中性，也更方便拼 SELECT。
        return "", []
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    return set_clause, values
