#
"""task 域枚举：任务执行器类型与背压原因。"""

from enum import Enum


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
