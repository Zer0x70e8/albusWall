# coding: utf-8
"""DTO 边界转换：类型收口、枚举容错、行取值、序列化、文本归一化。

本模块是 DTO 层唯一的转换工具集，契约集中在此，禁止各 DTO 自建。
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from enum import Enum
from typing import Any, TypeVar, overload
from uuid import UUID

__all__ = [
    "none_or",
    "to_uuid",
    "coerce_enum",
    "row_get",
    "to_json",
    "json_default",
    "normalize_tag_name",
]

E = TypeVar("E", bound=Enum)


def none_or(v, factory):
    if v is None:
        return factory()
    return v


# ---- 类型收口 ----
def to_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


# ---- 枚举容错（读路径）----
@overload
def coerce_enum(value: Any, enum_cls: type[E], default: E) -> E: ...


@overload
def coerce_enum(value: Any, enum_cls: type[E]) -> E | None: ...


def coerce_enum(
        value: Any,
        enum_cls: type[E],
        default: E | None = None,
) -> E | None:
    """读路径枚举容错：非法值降级到 ``default``，不阻塞。

    ``None`` 也降级到 ``default``（是否允许 None 由调用方 default 决定）。
    写路径**不**走本函数，直接 ``Enum(value)`` 抛 ``ValueError``。
    契约见 ``CONVENTIONS.md`` · 枚举容错。
    """
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


# ---- 行取值（from_row 可选字段）----
def row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """``sqlite3.Row`` 安全取值，缺列返回 ``default``。

    仅供 ``from_row`` 中的**可选/展示字段**；身份/必填字段请直取
    ``row[key]``，缺列即 schema/SELECT 错误，应暴露。
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


# ---- 序列化 ----
def to_json(dto: Any) -> str:
    return json.dumps(dto.to_dict(), ensure_ascii=False, default=json_default)


def json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---- 文本归一化 ----
def normalize_tag_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().lower()
