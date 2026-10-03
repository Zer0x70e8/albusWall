#
"""内部使用的任务项，仅调度器消费。

不属于公共契约。
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from albuswall.dto.task import ExecutorType


@dataclass(order=True)
class _Task:
    """优先队列中的任务项。

    排序只考虑 ``priority`` 与 ``seq``；其余字段 ``compare=False``，
    以便携带任意 payload（无需 payload 本身可比较）。
    """
    priority: int
    seq: int
    fn: Callable[..., Any] = field(compare=False)
    args: tuple[Any, ...] = field(compare=False)
    kwargs: dict[str, Any] = field(compare=False)
    executor: ExecutorType = field(compare=False)
    future: Future = field(compare=False)
