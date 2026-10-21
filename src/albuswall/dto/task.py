#
"""任务域 DTO：背压事件、任务视图与提交参数别名。

枚举（``ExecutorType`` / ``BackpressureReason`` / ``PermitKind``）属
共享内核，定义在 ``albuswall.common.enums.task``，本模块只负责转发
导入；``ExecutorArg`` 是 submit 入参的类型别名（DTO 侧契约），留在本模块。
"""

from dataclasses import dataclass, field
from typing import Callable, Literal, Union

from albuswall.common.enums import BackpressureReason, ExecutorType, PermitKind
from albuswall.dto.sentinel import UNSET, MaybeUnset


# 对外暴露的 submit 参数类型：
#   - 允许传 ExecutorType 枚举（推荐）
#   - 也允许传等值字符串字面量（"thread" / "process"），兼容旧调用
# 实现层用 ``ExecutorType(executor)`` 归一化并校验非法值。
ExecutorArg = Union[ExecutorType, Literal["thread", "process"]]


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
