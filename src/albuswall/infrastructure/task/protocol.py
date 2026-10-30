#
"""Unified Task Service 的公共接口契约。

设计原则
--------
本模块声明使用者与 TaskService 交互所必需的核心能力，以及这些能力
签名里用到的契约类型（公共 DTO）。契约类型与契约接口同处一文件，
保证「按协议编程」的读者不需要再跨包去翻类型定义。

契约类型（公共 DTO，定义在本模块）：
    * ``ExecutorArg``       —— ``submit`` 的 ``executor`` 入参别名
    * ``BackpressureEvent`` —— 背压回调 payload（``frozen``）
    * ``TaskServiceStats``  —— ``get_stats`` 返回观测快照
    * ``TaskSpec``          —— 结构化提交的写模型（``submit_spec`` 入参）

核心能力（``TaskServiceProtocol``）：
    * ``submit``                    —— 提交任务
    * ``submit_spec``               —— 按 ``TaskSpec`` 提交任务（结构化提交）
    * ``shutdown``                  —— 关闭服务
    * ``get_stats``                 —— 观测背压 / 计数
    * ``set_backpressure_callback`` —— 注册背压回调

以下内容**刻意不放入契约**，只由实现层（``service.py``）提供：

    * 调度循环 / 资源监控 / 动态并发调整
    * ``DynamicSemaphore`` 及各种 permit 语义
    * ``_put_back`` / ``_drain_queue`` / ``_validate_process_task`` 等
    * 队列大小、并发水位等只读属性

使用者若只依赖本协议，可以完全不知道实现细节；
需要更细粒度的观测时，再直接引用具体实现的属性。
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Literal,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)

from albuswall.common.enums import BackpressureReason, ExecutorType
from albuswall.dto.sentinel import UNSET, MaybeUnset

# ============================================================
# 契约类型（公共 DTO）
# ============================================================
# 对外暴露的 submit 参数类型：
#   - 允许传 ExecutorType 枚举（推荐）
#   - 也允许传等值字符串字面量（"thread" / "process"），兼容旧调用
# 实现层用 ``ExecutorType(executor)`` 归一化并校验非法值。
ExecutorArg = Union[ExecutorType, Literal["thread", "process"]]


class PermitKind(str, Enum):
    """permit 种类，用于 ``PERMIT_WAIT`` 事件的细分。"""
    GLOBAL = "global"
    PROCESS = "process"


@dataclass(frozen=True, slots=True)
class BackpressureEvent:
    """背压回调收到的事件 payload。

    契约：
    * **只读回调 payload**，``frozen=True``。
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


BackpressureCallback = Callable[[BackpressureEvent], None]


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


# ============================================================
# 契约接口
# ============================================================
@runtime_checkable
class TaskServiceProtocol(Protocol):
    """统一任务服务的最小契约。"""

    def submit(
            self,
            fn: Callable[..., Any],
            *args: Any,
            executor: ExecutorArg = ExecutorType.THREAD,
            priority: int = 0,
            correlation_id: Any = UNSET,
            **kwargs: Any,
    ) -> Future:
        """提交一个任务到统一优先级队列，返回 Future。

        ``executor`` 接受 :class:`ExecutorType` 或其等值字符串
        （``"thread"`` / ``"process"``）；实现层会用
        ``ExecutorType(executor)`` 归一化，非法值抛 ``ValueError``。

        :raises TaskServiceShutdownError: 服务已关闭 / 正在关闭（:class:`RuntimeError` 的子类，旧 ``except RuntimeError`` 仍能接住）
        :raises TypeError: process 任务不可 pickle，或使用了不支持的形式
            （lambda / 局部函数 / 绑定方法）
        :raises ValueError: ``executor`` 不是 ``"thread"`` 或 ``"process"``
        :raises BackpressureError: 队列已满（拒绝式背压），见下方说明

        BackpressureError 的两种携带方式
        --------------------------------
        调用方**必须同时**处理下列两种情形，只捕获其中一种都会漏掉错误：

        1. **同步抛出**：调用 ``submit()`` 的瞬间若队列已满，会立即抛出
           ``BackpressureError``，其 ``reason`` 为
           :attr:`BackpressureReason.QUEUE_FULL`；
           此时 ``submit()`` 不会返回 ``Future``。

        2. **由返回的 Future 携带**：任务虽然成功入队，但在调度过程中若
           需要被重新放回队列（例如 shutdown / 重新入队），而此时队列再
           次被填满，任务会被丢弃，错误通过 ``Future`` 传递，
           ``reason`` 为 :attr:`BackpressureReason.PUT_BACK_FAILED`。
           调用方需在 ``future.result()`` / ``future.exception()`` 时感知。

        也就是说：背压既可能表现为 ``submit`` 的同步异常，也可能表现为
        返回的 ``Future`` 上的异常；两者语义上都属于「拒绝式背压」，
        ``reason`` 用于区分具体场景。

        Shutdown 语义（第三条失败通道，与上面同构）
        ------------------------------------------
        ``submit`` 成功返回 ``Future`` **不等于** 任务一定被执行。若任务
        已入队、但还没被调度线程取走，而服务随后被 ``shutdown()``，
        队列里剩余的任务会被丢弃，并以
        ``TaskServiceShutdownError("TaskService is shutting down")`` 结束：

        1. **同步抛出**：``submit()`` 时服务已关闭，立即
           ``TaskServiceShutdownError``，此时没有 ``Future``。
        2. **由 Future 携带**：入队后才发生 shutdown，任务被丢弃，
           调用方需在 ``future.result()`` / ``future.exception()`` 时感知。

        ``TaskServiceShutdownError`` 继承 ``RuntimeError``，因此下面的写法
        两种都成立；用具体类可以把它和别的运行时错误分开处理：

        .. code-block:: python

            try:
                fut = submit(...)          # 同步
            except BackpressureError:
                ...                        # 队列满，稍后重试
            except TaskServiceShutdownError:
                ...                        # 服务已关闭，别再试了
            else:
                try:
                    fut.result()           # 异步
                except (BackpressureError, TaskServiceShutdownError):
                    ...                    # 丢弃 / 拒绝，任务未执行

        背压与 shutdown 是同一个形状：背压 = 「拒绝」，shutdown = 「丢弃」，
        两者都可能同步发生（此时没有 ``Future``），也可能由 ``Future`` 携带。

        ``correlation_id`` 的传递路径
        -----------------------------
        ``correlation_id`` 是调用方给自己的实体（如 source 的 seq）加的锚点，
        服务端只做透传，不解释内容。它的**权威传递路径是**
        :attr:`BackpressureEvent.correlation_id`：覆盖同步抛错
        （``QUEUE_FULL``，此时连 ``Future`` 都没有）与异步 ``Future`` 异常
        （``PUT_BACK_FAILED``）两条通道。**``Future`` 上不挂该属性** ——
        不要试图从 ``Future`` 反查，异常路径下它根本不存在。

        典型用法（回调里按锚点回收自己的 pending 记录）::

            service.set_backpressure_callback(
                lambda evt: on_bp(evt.correlation_id, evt.reason))
            service.submit(fn, correlation_id=task.seq)

        ``fn`` 的参数名约束
        -------------------
        ``executor`` / ``priority`` / ``correlation_id`` 是本方法的
        keyword-only 参数，会与 ``fn`` 自己的同名参数冲突（``**kwargs`` 里
        出现这些键会在调用点抛
        ``TypeError: got multiple values for keyword argument``）。
        因此 **``fn`` 不得有名为 ``executor`` / ``priority`` /
        ``correlation_id`` 的参数**；需要传同名值时请改名（如
        ``executor_name``）或改用 :class:`TaskSpec` 的字段。
        """
        ...

    def submit_spec(self, spec: TaskSpec) -> Future:
        """按 :class:`TaskSpec` 提交任务。

        行为与 :meth:`submit` **完全一致**，校验 / 背压 / 异常全部透传：
        契约以 :meth:`submit` 的 docstring 为唯一来源，此处不复述。

        ``spec`` 走 process 执行器时，``fn`` 必须是模块级顶层函数
        （不能 lambda / 闭包 / 局部函数 / 绑定方法，也不能定义在
        ``__main__``），``args`` / ``kwargs`` 必须可 pickle。
        """
        ...

    def shutdown(self, wait: bool = True) -> None:
        """关闭服务：停调度、清队列、关线程/进程池。"""
        ...

    def get_stats(self) -> TaskServiceStats:
        """返回背压 / 提交 / 拒绝等内部计数的快照。

        各字段口径（尤其 ``submitted`` 含被拒绝的尝试、
        ``backpressure_wait`` 每任务最多 +1）见 :class:`TaskServiceStats`。
        """
        ...

    def set_backpressure_callback(
            self, cb: Optional[BackpressureCallback]
    ) -> None:
        """注册/注销背压事件回调。

        ``cb`` 会收到一个 :class:`BackpressureEvent` 实例（不是 dict）。
        必填字段任何 reason 都有；附加字段随 ``reason`` 变化：

        - ``QUEUE_FULL``      -> ``seq`` / ``priority``
        - ``PUT_BACK_FAILED`` -> ``seq``
        - ``PERMIT_WAIT``     -> ``seq`` / ``kind`` / ``waited``

        可选字段用 :data:`MaybeUnset` 表达：未携带时为 ``UNSET``；
        判断"是否携带"用 ``evt.seq is not UNSET``，而不是 ``"seq" in evt``。

        ``seq`` 是 TaskService 内部序号，仅用于日志/调试关联；调用方若需
        反查自己的实体，请用 ``evt.correlation_id``（见 :meth:`submit`）。
        """
        ...
