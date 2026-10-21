#
"""仓储层返回值类型别名。

仓储的批量查询 / 统计可能以两种标识做键：

* ``assets.id`` —— 仅内部流程（缩略图按 id 批量读写、回收站按 id 批量恢复）。
  这类映射仍用 ``dict[int, T]``，不对外暴露 id 语义；
* ``uuid`` —— 对外标识。为避免“有的 repo 用 ``UUID`` 做键、有的用 ``str``
  做键”导致调用方 ``mapping.get(x)`` 静默取不到值，统一为
  :data:`UuidKeyed`：键是 canonical ``str(UUID)``。

为什么键是 ``str`` 而不是 ``UUID``：

* SQL / UI / 日志侧天然是字符串，``UUID`` 只在 DTO 字段上出现；
* ``str`` 是 hashable 且可跨进程 / 跨 JSON 序列化，作为映射键更稳；
* 统一后 ``counts.get(str(tag.uuid), 0)`` 与 ``paths.get(str(asset.uuid))``
  是同一种写法，不会因类型不一致而静默 miss。
"""

from __future__ import annotations

from typing import TypeAlias, TypeVar

__all__ = ["UuidKeyed"]

T = TypeVar("T")

# 以 canonical uuid 字符串（``str(UUID)``）为键的批量结果。
UuidKeyed: TypeAlias = dict[str, T]
