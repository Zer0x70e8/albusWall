#
"""Tag 域 —— 命令侧 DTO（Command）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* 本模块只放**写模型**（repo 写方法入参），只由命令路径产生与消费；
* 不提供 ``from_row``（命令 DTO 禁止 from_row），不声明 ``COLUMNS``；
* 查询路径禁止 import 本模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Optional
from uuid import UUID

from albuswall.dto.sentinel import UNSET, PatchField
from albuswall.dto.utils import normalize_tag_name

__all__ = [
    "TagCreate",
    "TagUpdate",
]


@dataclass
class TagCreate:
    """创建输入：只有用户能提供的字段。

    契约：``parent_uuid`` 对外为 ``Optional[UUID]``；
    repo 的 INSERT 绑定负责 ``UUID→str``。
    """
    name: str
    parent_uuid: Optional[UUID] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    sort_order: int = 0
    # 派生字段：调用方不能传，构造后由 __post_init__ 写入
    norm_name: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tag name must be non-empty")
        self.norm_name = normalize_tag_name(self.name)


@dataclass
class TagUpdate:
    """PATCH 输入：UNSET 跳过，显式 None 置 NULL。"""
    name: PatchField[str] = UNSET
    display_name: PatchField[str] = UNSET
    description: PatchField[str] = UNSET
    sort_order: PatchField[int] = UNSET

    def __post_init__(self) -> None:
        if self.name is not UNSET and self.name is not None:
            # noinspection unresolved-references
            if not self.name.strip():
                raise ValueError("tag name must be non-empty")

    @property
    def derived_norm_name(self) -> Optional[str]:
        """name 有值时返回归一化结果；UNSET / None 返回 None。

        用 property 而非 field：不污染 ``is_empty()`` 的遍历，
        也不会让调用方误传 ``norm_name`` 进来。
        """
        if self.name is UNSET or self.name is None:
            return None
        # noinspection bad-argument-type
        return normalize_tag_name(self.name)

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is UNSET for f in fields(self))
