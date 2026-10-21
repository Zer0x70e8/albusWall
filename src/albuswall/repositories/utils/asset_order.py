#
"""资产列表排序契约（单一来源）。

``ViewRepository``（scope / 相簿）与 ``TagRepository``（标签内资产）的
「资产顺序」必须完全一致，否则同一张图在不同入口的前后邻居会对不上。
排序 SQL 只在本模块维护一份，两个仓储显式 import。

契约（与 schema 的排序索引对齐）::

    active  : {p}taken_at IS NULL, {p}taken_at DESC, {p}id DESC
    deleted : {p}deleted_at IS NULL, {p}deleted_at DESC, {p}id DESC

``{p}`` 是表别名前缀（``""`` 或 ``"a."``）。``id`` 作为 tie-break，
保证顺序唯一确定，索引见 ``idx_assets_active_taken_id`` /
``idx_assets_deleted_deleted_at_id``。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ALLOWED_PREFIXES",
    "ASSET_ORDER_SQL",
    "order_by",
]

ASSET_ORDER_SQL: Final[dict[str, str]] = {
    "active": "{p}taken_at IS NULL, {p}taken_at DESC, {p}id DESC",
    "deleted": "{p}deleted_at IS NULL, {p}deleted_at DESC, {p}id DESC",
}

# 当前只有「整表查询」与「assets 别名 a」两种前缀；白名单防止拼接注入。
ALLOWED_PREFIXES: Final[frozenset[str]] = frozenset(("", "a."))


def order_by(scope: str, prefix: str = "") -> str:
    """返回资产列表的 ``ORDER BY`` 子句。

    Args:
        scope: ``"active"`` / ``"deleted"``。
        prefix: 表别名前缀，只能是 ``""`` 或 ``"a."``。

    Raises:
        ValueError: scope 或 prefix 不在白名单内。
    """
    if prefix not in ALLOWED_PREFIXES:
        raise ValueError(f"unsupported prefix: {prefix!r}")
    try:
        template = ASSET_ORDER_SQL[scope]
    except KeyError:
        raise ValueError(f"unsupported scope: {scope!r}") from None
    return template.format(p=prefix)
