#
"""标签域的类型化异常（``TagError`` 家族）。

全部继承 :class:`~albuswall.service.errors.ServiceError`，因此命令层可以按
"服务层异常一棵树"统一翻译（``service/command/dispatch_errors.py``），不必
为每个标签异常各写一条映射；领域载荷（``uuid`` / ``name`` / ``parent_uuid``）
保留在实例上，供 presenter 生成文案与撤销提示。

名字与旧实现一致（``TagNotFound`` / ``TagNameConflict`` / ``TagCycle`` …），
迁移只改了基类：``Exception`` → ``ServiceError``。
"""

from __future__ import annotations

from typing import Optional, Sequence
from uuid import UUID

from albuswall.service.errors import ServiceError

__all__ = [
    "TagCycle",
    "TagError",
    "TagHasChildren",
    "TagMoveConflict",
    "TagNameConflict",
    "TagNotFound",
    "TagParentNotFound",
    "TagRestoreConflict",
]

_UUIDLike = UUID | str


class TagError(ServiceError):
    """Tag 业务错误基类（服务层异常家族的一员）。"""


class TagNotFound(TagError):
    def __init__(self, uuid: str):
        super().__init__(f"tag not found: {uuid}")
        self.uuid = uuid


class TagParentNotFound(TagError):
    def __init__(self, parent_uuid: str):
        super().__init__(f"parent tag not found: {parent_uuid}")
        self.parent_uuid = parent_uuid


class TagNameConflict(TagError):
    def __init__(
            self,
            name: str,
            existing_uuid: Optional[_UUIDLike] = None,
    ):
        super().__init__(f"tag name conflict: {name!r}")
        self.name = name
        self.existing_uuid = existing_uuid


class TagHasChildren(TagError):
    def __init__(self, uuid: str, children_uuids: Sequence[str]):
        super().__init__(f"tag has children: {uuid} -> {list(children_uuids)}")
        self.uuid = uuid
        self.children_uuids = tuple(children_uuids)


class TagCycle(TagError):
    def __init__(self, tag_uuid: str, new_parent_uuid: Optional[str]):
        super().__init__(f"cycle detected: {tag_uuid} -> {new_parent_uuid}")
        self.tag_uuid = tag_uuid
        self.new_parent_uuid = new_parent_uuid


class TagMoveConflict(TagError):
    def __init__(self, tag_uuid: str, new_parent_uuid: Optional[str]):
        super().__init__(
            f"cannot move tag {tag_uuid} to {new_parent_uuid}: name conflict"
        )
        self.tag_uuid = tag_uuid
        self.new_parent_uuid = new_parent_uuid


class TagRestoreConflict(TagError):
    def __init__(self, uuid: str):
        super().__init__(f"cannot restore tag (name conflict): {uuid}")
        self.uuid = uuid
