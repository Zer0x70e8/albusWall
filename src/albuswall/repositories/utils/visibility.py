#
"""资产可见性条件（跨仓储单一来源）。

本项目里「资产可见（active）」不是单表谓词，而是两段：

1. 资产自身未软删（``assets.is_deleted = 0``）；
2. 资产所属导入源未软删（``ingest_source.is_deleted = 0``）——
   源被软删意味着整源下线，其资产即使行还在也不再对外可见。

第二条如果只写在 ``scope`` 列表查询里、不写进单资产读取 / 相簿成员 /
标签统计，就会出现「列表里没有，但按 uuid 能打开」或「相簿计数包含
已下线资产」这类不报错的错位。因此所有「可见资产」条件统一由本模块
生成，禁止各仓储各拼一份。

用 EXISTS 子查询而不是 JOIN 的原因：

* 不改动调用方的 FROM / ORDER BY 结构，现有 partial index 继续命中；
* ``ingest_source`` 通常只有几行，EXISTS 的代价可忽略；
* 对已经写死 ``a.is_deleted = 0`` 的查询可以原地替换成同一段条件。
"""

from __future__ import annotations

from typing import Final

__all__ = ["ALLOWED_ALIASES", "active_asset_where"]

# 当前只允许这几种表别名；白名单避免把用户输入拼进 SQL 标识符位置。
ALLOWED_ALIASES: Final[frozenset[str]] = frozenset(("", "a", "asset", "cover"))


def active_asset_where(alias: str = "") -> str:
    """返回「可见资产」的 SQL 条件片段。

    Args:
        alias: assets 表的别名；``""`` 表示用表名 ``assets``。
            只接受 :data:`ALLOWED_ALIASES` 中的值。

    Raises:
        ValueError: alias 不在白名单内。
    """
    if alias not in ALLOWED_ALIASES:
        raise ValueError(f"unsupported assets alias: {alias!r}")
    p = f"{alias}." if alias else "assets."
    return (
        f"{p}is_deleted = 0 "
        f"AND EXISTS (SELECT 1 FROM ingest_source AS s "
        f"            WHERE s.id = {p}source_id AND s.is_deleted = 0)"
    )
