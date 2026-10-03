#
""""""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypedDict


class ExecutorType(str, Enum):
    """任务执行器类型。

    继承 ``str`` 保证：
      - 与既有 ``executor="thread"`` 字符串调用/比较完全兼容
      - 序列化 / JSON 输出仍是 ``"thread"`` / ``"process"``
      - ``ExecutorType("Thread")`` 会直接抛 ``ValueError``，运行时有防拼写兜底
    """
    THREAD = "thread"
    PROCESS = "process"


class BackpressureReason(str, Enum):
    """背压事件的原因分类。

    - ``QUEUE_FULL``：submit 时队列满，同步抛 ``BackpressureError``
    - ``PUT_BACK_FAILED``：任务重新入队时队列满，错误由 Future 携带
    - ``PERMIT_WAIT``：等待 permit 超过阈值（观察型，不拒绝任务）
    """
    QUEUE_FULL = "queue_full"
    PUT_BACK_FAILED = "put_back_failed"
    PERMIT_WAIT = "permit_wait"


class PermitKind(str, Enum):
    """permit 种类，用于 ``PERMIT_WAIT`` 事件的细分。"""
    GLOBAL = "global"
    PROCESS = "process"


class _BackpressureEventRequired(TypedDict):
    """``BackpressureEvent`` 的必填字段。"""
    reason: BackpressureReason
    queue_size: int
    queue_max: int
    concurrency_current: int
    concurrency_max: int
    ts: float


class BackpressureEvent(_BackpressureEventRequired, total=False):
    """背压回调收到的事件 payload。

    必填字段见 :class:`_BackpressureEventRequired`；
    以下字段按 ``reason`` 动态附加，故为可选：

    - ``QUEUE_FULL``      -> ``seq`` / ``priority``
    - ``PUT_BACK_FAILED`` -> ``seq``
    - ``PERMIT_WAIT``     -> ``seq`` / ``kind`` / ``waited``
    """
    seq: int
    priority: int
    kind: PermitKind
    waited: float


class TaskServiceStats(TypedDict):
    """``TaskService.get_stats()`` 返回的快照。"""
    submitted: int
    rejected: int
    backpressure_reject: int
    backpressure_wait: int
    last_backpressure_ts: float


@dataclass(order=True)
class Task:
    """优先级队列中的任务项"""
    priority: int
    seq: int
    fn: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    executor: ExecutorType = field(compare=False)
    # future: Optional[Future] = field(compare=False)
    #
    # def is_void(self):
    #     if self.future is None:
    #         return True
    #     return False
