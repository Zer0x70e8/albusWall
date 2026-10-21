#
""""""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, Union

from albuswall.dto.sentinel import UNSET, MaybeUnset


class ExecutorType(str, Enum):
    """任务执行器类型。

    继承 ``str`` 保证：
      - 与既有 ``executor="thread"`` 字符串调用/比较完全兼容
      - 序列化 / JSON 输出仍是 ``"thread"`` / ``"process"``
      - ``ExecutorType("Thread")`` 会直接抛 ``ValueError``，运行时有防拼写兜底
    """
    THREAD = "thread"
    PROCESS = "process"


# 对外暴露的 submit 参数类型：
#   - 允许传 ExecutorType 枚举（推荐）
#   - 也允许传等值字符串字面量（"thread" / "process"），兼容旧调用
# 实现层用 ``ExecutorType(executor)`` 归一化并校验非法值。
ExecutorArg = Union[ExecutorType, Literal["thread", "process"]]


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


@dataclass(frozen=True, slots=True)
class BackpressureEvent:
    """背压回调收到的事件 payload。

    契约：
    * **只读回调 payload**，``frozen=True``（见 ``CONVENTIONS.md`` · 可变性）。
    * ``TypedDict`` 已撤销，全改 dataclass；原 ``_BackpressureEventRequired``
      与 ``BackpressureEvent`` 合并为单类。
    * 按 ``reason`` 动态附加的字段用 :data:`MaybeUnset`（``UNSET`` = 未携带，
      ``None`` = 显式置空，否则为具体值）。消费方原 ``"seq" in event``
      的键存在性判断应改为 ``event.seq is not UNSET``。

    字段对应：
    * 必填 —— 任何 reason 都有；
    * 可选 —— 按 ``reason`` 动态附加：

        - ``QUEUE_FULL``      -> ``seq`` / ``priority``
        - ``PUT_BACK_FAILED`` -> ``seq``
        - ``PERMIT_WAIT``     -> ``seq`` / ``kind`` / ``waited``
    """

    # 必填
    reason: BackpressureReason
    queue_size: int
    queue_max: int
    concurrency_current: int
    concurrency_max: int
    ts: float

    # 按 reason 动态附加
    seq: MaybeUnset[int] = UNSET
    priority: MaybeUnset[int] = UNSET
    kind: MaybeUnset[PermitKind] = UNSET
    waited: MaybeUnset[float] = UNSET


@dataclass(frozen=True, slots=True)
class TaskServiceStats:
    """``TaskService.get_stats()`` 返回的快照。

    契约：``TypedDict`` 已撤销，全改 dataclass；全字段必填，避免
    构造侧漏字段被静默吞掉。消费方从 ``stats["submitted"]`` 改为
    ``stats.submitted``。
    """
    submitted: int
    rejected: int
    backpressure_reject: int
    backpressure_wait: int
    last_backpressure_ts: float


@dataclass(order=True)
class Task:
    """面向外部消费者的任务元信息视图。

    只描述"提交了什么"，不含调度期状态（``future`` / 完成回调等）。
    调度器内部的完整任务项见
    :class:`albuswall.infrastructure.task._task._Task`。
    """
    priority: int
    seq: int
    fn: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    executor: ExecutorType = field(compare=False)
