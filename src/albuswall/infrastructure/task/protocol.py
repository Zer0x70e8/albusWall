#
"""Unified Task Service 的公共接口契约。

设计原则
--------
本模块**只**声明使用者与 TaskService 交互所必需的核心能力：

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
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from albuswall.common.enums import ExecutorType
from albuswall.dto.sentinel import UNSET
from albuswall.dto.task import (
    BackpressureEvent,
    ExecutorArg,
    TaskServiceStats,
    TaskSpec,
)

BackpressureCallback = Callable[[BackpressureEvent], None]


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
