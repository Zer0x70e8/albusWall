#
"""标签域服务：``tags`` + ``tag_aliases``（本模块只做再导出）。

分层
----
::

    service.py     TagService   —— 标签树的生命周期（CRUD / 树 / 路径 / 搜索 / 软删恢复）
    aliases.py     AliasService —— 路径解析的"软链接"（禁环 / 不指向别名）
    errors.py      TagError 家族（统一继承 ServiceError）

两张表、两套不变量，所以两个服务；它们共享同一个 ``TagRepository``，
互不 import。

与资产域的关系
--------------
``asset_tags``（资产挂了哪些标签）**不在**这里 —— 那是资产写命令的职责，见
:mod:`albuswall.service.asset.command.tagging`。本包只保证"标签本身"是干净的：
``assets`` 侧永远只引用 ``tags.uuid``，别名仅参与路径解析。
"""

from .aliases import ALIAS_SUFFIX, AliasCycle, AliasService, resolve_alias_in_path
from .errors import (
    TagCycle,
    TagError,
    TagHasChildren,
    TagMoveConflict,
    TagNameConflict,
    TagNotFound,
    TagParentNotFound,
    TagRestoreConflict,
)
from .service import RestoreResult, TagService

__all__ = [
    "ALIAS_SUFFIX",
    "AliasCycle",
    "AliasService",
    "RestoreResult",
    "TagCycle",
    "TagError",
    "TagHasChildren",
    "TagMoveConflict",
    "TagNameConflict",
    "TagNotFound",
    "TagParentNotFound",
    "TagRestoreConflict",
    "TagService",
    "resolve_alias_in_path",
]
