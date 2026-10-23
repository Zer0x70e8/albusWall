#
"""任务域 DTO：背压事件、任务提交描述与提交参数别名。

枚举（``ExecutorType`` / ``BackpressureReason`` / ``PermitKind``）属
共享内核，定义在 ``albuswall.common.enums.task``，本模块只负责转发
导入；``ExecutorArg`` 是 submit 入参的类型别名（DTO 侧契约），留在本模块。

``TaskSpec`` 是**唯一的**任务提交描述（写模型）；任务完成后要观测什么，
直接看对应 ``Future`` 的结果 —— 不再单独维护一个"任务视图"DTO。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Union

from albuswall.common.enums import BackpressureReason, ExecutorType, PermitKind
from albuswall.dto.sentinel import UNSET, MaybeUnset

__all__ = [
    "BackpressureEvent",
    "ExecutorArg",
    "TaskServiceStats",
    "TaskSpec",
]


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
    * ``correlation_id`` —— 调用方在 ``submit(correlation_id=...)`` 传入的
      锚点原样回传；未传时为 ``UNSET``。**这是反向关联调用方实体的权威
      通道**：``QUEUE_FULL`` 时 ``Future`` 还不存在，回调是本事件唯一的
      出口（``Future`` 上不挂该属性，故任何路径都只认这里）。
    * 可选 —— 按 ``reason`` 动态附加：

        - ``QUEUE_FULL``      -> ``seq`` / ``priority``
        - ``PUT_BACK_FAILED`` -> ``seq``
        - ``PERMIT_WAIT``     -> ``seq`` / ``kind`` / ``waited``

    ``seq`` 是 TaskService **内部序号**，仅用于日志 / 调试关联；调用方若需
    反查自己的实体（source、批次、请求……），请用 ``correlation_id``。
    ``seq`` 随服务生命周期递增，跨重启 / 跨实例没有意义。
    """

    # 必填
    reason: BackpressureReason
    queue_size: int
    queue_max: int
    concurrency_current: int
    concurrency_max: int
    ts: float

    # 调用方锚点（任何 reason 都可能携带，故不放进"按 reason 动态附加"组）
    correlation_id: Any = UNSET

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

    字段口径：

    * ``submitted`` —— **成功入队**的任务数。与 ``rejected`` **互斥**：
      被背压拒绝的那次提交只计入 ``rejected``，不计入 ``submitted``。
      （自增点在 ``queue.put`` 成功之后。）
    * ``rejected`` —— ``submit`` 阶段因队列满被同步拒绝的次数
      （``BackpressureReason.QUEUE_FULL``）。不含 ``PUT_BACK_FAILED``
      （任务已入队、重新入队时被丢弃），那个只计入
      ``backpressure_reject``。
    * ``backpressure_reject`` —— 所有**拒绝式**背压事件数
      （``QUEUE_FULL`` + ``PUT_BACK_FAILED``）；因此它 ≥ ``rejected``。
    * ``backpressure_wait`` —— ``PERMIT_WAIT`` 事件数。**每个任务最多
      计 1 次**（等待超过 ``wait_warn_threshold`` 后成功拿到 permit 时
      记录），不是等待时长，也不是等待轮次。
    * ``last_backpressure_ts`` —— 最近一次**任意**背压事件的
      ``time.time()``；单值字段，不区分 reason。

    边界：``submit`` 抛 ``RuntimeError``（服务已关闭）的那次提交既不计
    ``submitted`` 也不计 ``rejected`` —— 它连入队都没尝试。shutdown 丢弃
    的已入队任务计入 ``submitted``（它们确实入过队），但不影响
    ``rejected``。
    """

    submitted: int
    rejected: int
    backpressure_reject: int
    backpressure_wait: int
    last_backpressure_ts: float


@dataclass
class TaskSpec:
    """提交任务用的**写模型**（write model），``submit_spec`` 的唯一入参。

    本类只负责把「提交什么」聚合为一个可传递的值：``submit_spec`` 的行为
    与 ``submit`` **完全一致**（executor 归一化、process 任务校验、关闭检查、
    背压、异常语义全部透传），校验只在 ``submit`` 一处发生，这里不做任何
    重复校验。

    可变性与 pickle 契约
    --------------------
    * **不加 frozen、不加 slots**：这是可写的入参模型，不是只读回调 payload。
      ``slots`` 还会把「pickle 往返是否保留字段」绑到解释器版本上（Python
      3.10 上 slot 字段不进 ``__reduce_ex__`` 的 state，往返后字段静默丢失），
      写模型不必背这个包袱。
    * ``fn`` / ``args`` / ``kwargs`` 统一 ``compare=False``：不参与
      ``__eq__``，避免把不可比较的 payload 拖进比较逻辑。
      **副作用要知道**：剩下参与比较的只有 ``executor`` 与 ``priority``，
      两个 payload 不同的 spec 也会比较相等 —— 所以 **不要** 把 ``TaskSpec``
      塞进 ``set`` / 当 dict key（非 frozen 且 ``eq=True``，``__hash__`` 为
      ``None``，本身也不可 hash）。
    * ``pickle`` 自检**不在** ``__post_init__`` 里做：构造是热路径，thread
      任务根本不需要 pickle；校验时机是 ``submit``（process 任务才校验）。

    pickle 约束（``executor=ExecutorType.PROCESS`` 时生效）
    ----------------------------------------------------
    * ``fn`` 必须是**模块级顶层函数**：不能是 lambda、闭包、局部函数或
      绑定方法。
    * ``fn`` **不能定义在 ``__main__``**：进程池用 spawn 启动，子进程里的
      ``__main__`` 是 ``__mp_main__``，反序列化会得到
      ``AttributeError: module '__main__' has no attribute ...``。
      （父进程侧 ``pickle.dumps`` 预检*发现不了*这个问题，它只在子进程
      ``loads`` 时炸。）
    * ``args`` / ``kwargs`` 必须可 pickle。

    ``fn`` 的参数名约束
    -------------------
    ``submit`` 的 ``executor`` / ``priority`` 是 keyword-only 参数，
    ``submit_spec`` 会把本类的 ``executor`` / ``priority`` 字段当作它们、
    把 ``kwargs`` 整体展开。若 ``fn`` 自身有名为 ``executor`` 或
    ``priority`` 的参数，``kwargs`` 与之撞名会抛
    ``TypeError: got multiple values for keyword argument`` —— 这是
    ``submit`` 的固有语义约束（不是本类引入的），所以本类不做键校验，
    只在此文档化：**``fn`` 不得有名为 ``executor`` / ``priority`` 的参数**。
    """

    fn: Callable[..., Any] = field(compare=False)
    args: tuple[Any, ...] = field(compare=False, default=())
    kwargs: dict[str, Any] = field(compare=False, default_factory=dict)
    executor: ExecutorArg = ExecutorType.THREAD
    priority: int = 0
