#
"""Tag 域 DTO 包（Command / Query 分包）。

按 ``dto/CONVENTIONS.md`` · DTO 归属与命名 分包：

* ``dto.tag.query``   —— 读模型与视图 DTO（``Tag`` / ``TagTreeNode`` / ``TagWithStats``）；
* ``dto.tag.command`` —— 写模型（``TagCreate`` / ``TagUpdate``）。

本 ``__init__`` 只做**显式**再导出（无通配、无新增语义），新代码建议直接从
所属子模块 import，以便在 import 行上就能看出读写侧。
"""

from .command import TagCreate, TagUpdate
from .query import Tag, TagMatch, TagTreeNode, TagWithStats

__all__ = [
    "Tag",
    "TagCreate",
    "TagMatch",
    "TagTreeNode",
    "TagUpdate",
    "TagWithStats",
]
