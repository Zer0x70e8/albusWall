#
"""Unified Task Service 的公共接口契约。

设计原则
--------
本模块**只**声明使用者与 TaskService 交互所必需的核心能力：

    * ``submit``                    —— 提交任务
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

from albuswall.dto.task import (
    BackpressureEvent,
    ExecutorType,
    TaskServiceStats,
)

BackpressureCallback = Callable[[BackpressureEvent], None]


@runtime_checkable
class TaskServiceProtocol(Protocol):
    """统一任务服务的最小契约。"""

    def submit(
            self,
            fn: Callable[..., Any],
            *args: Any,
            executor: ExecutorType = ExecutorType.THREAD,
            priority: int = 0,
            **kwargs: Any,
    ) -> Future:
        """提交一个任务到统一优先级队列，返回 Future。

        ``executor`` 接受 :class:`ExecutorType` 或其等值字符串
        （``"thread"`` / ``"process"``）；实现层会用
        ``ExecutorType(executor)`` 归一化，非法值抛 ``ValueError``。

        :raises RuntimeError: 服务已关闭
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
        """
        ...

    def shutdown(self, wait: bool = True) -> None:
        """关闭服务：停调度、清队列、关线程/进程池。"""
        ...

    def get_stats(self) -> TaskServiceStats:
        """返回背压 / 提交 / 拒绝等内部计数的快照。"""
        ...

    def set_backpressure_callback(
            self, cb: Optional[BackpressureCallback]
    ) -> None:
        """注册/注销背压事件回调。

        ``cb`` 会收到 :class:`BackpressureEvent` 形态的 dict，
        必填字段见该 TypedDict 定义，附加字段随 ``reason`` 变化。
        """
        ...
