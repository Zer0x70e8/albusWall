#
"""把服务的工作搬回主循环线程执行 —— **服务层**使用的线程边界原语。

为什么在服务层，而不是命令层
---------------------------
handler 跑在 IPC 的工作线程（``ipc-conn``，见 ``handlers/_protocol.py``
第 8 条），而 ``TrackingState`` 只允许属主线程写
（``service/source/state/tracking.py`` 的线程契约）。``Signal`` 是**同步
派发**（``utils/signals.py``），所以 ``SourceService.create`` 在 IPC 线程里
写库 + ``emit`` 等于把状态层（及其 runtime）的写操作搬到了别人的线程上 ——
而且是在 DB 已经提交之后才炸，属于"半完成"。

因此线程边界画在**服务的写入口**：public 方法负责"回主线程"，私有实现
（``_create`` / ``_update`` / ``_delete``）只做业务。命令层、``Dispatcher``、
handler 全部保持原样（仍在子线程、仍同步）。

语义
----
``call(fn)`` 三条分支，顺序即优先级：

1. **已在该线程 → 直接执行**。UI 写路径本来就是主线程，这条路零开销，
   同时这也是自锁保护：``call_soon`` 排队等自己会直接死锁。
2. **没有可用主循环 → 直接执行**。单测里的替身循环（只实现 ``run`` /
   ``quit`` 的 ``MainLoop`` 子类）与未装配主循环的场景必须能跑；行为等于
   没有本类时的样子。
3. **其余情况 → 排队并阻塞等待**，超时抛 :class:`MainThreadTimeout`。

防死锁规则（调用方必须遵守）
----------------------------
* **主线程永远不同步等待 worker**。一旦出现"主线程等 worker、worker 又
  通过本类等主线程"的环就是死锁，超时只能把死锁变成报错，不是解法。
* 本类只用于**写 / 变更**入口；读路径（纯仓储查询）不平移，否则读命令
  也要排队，白白放大延迟。

生命周期
--------
* :meth:`start` 挂在 ``Application.on_loop`` 上：该 hook 与 ``loop.run()``
  同线程、紧邻其前执行（``core/application.py``），因此那一刻的
  ``current_thread()`` 就是主循环属主。**不能**简单用
  ``threading.main_thread()`` 判定 —— headless 循环不保证跑在进程主线程。
* :meth:`close` 挂在 ``Application.on_exit`` 上，且要排在
  ``ipc_request_service.shutdown()`` 之后：先摘 handler（不再受理新请求），
  再取消在途调用，调用方立刻拿到错误，而不是干等超时。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, TypeVar

from albuswall.log import getLogger

from .errors import MainLoopUnavailable, MainThreadTimeout

__all__ = ["DEFAULT_TIMEOUT", "MainLoopService"]

T = TypeVar("T")

_logger = getLogger(__name__)

#: 等待主循环响应的上限（秒）。
#:
#: 必须**明显小于** IPC 客户端超时（``IpcClient.timeout`` 默认 5.0，见
#: ``infrastructure/ipc/_client.py``），否则客户端先超时，用户拿到的是没信息量
#: 的客户端异常，而不是"主循环被卡住"这个可诊断结论。
DEFAULT_TIMEOUT = 3.0


class MainLoopService:
    """主循环线程执行器（容器 key：``main_loop_service``）。

    构造只做绑定，不启动任何线程；``loop`` 允许为 ``None``（未装配 / 测试），
    此时退化为同步内联执行。
    """

    def __init__(
            self,
            loop: Any = None,
            *,
            timeout: float = DEFAULT_TIMEOUT,
            owner: threading.Thread | None = None,
    ) -> None:
        self._loop = loop
        self._timeout = float(timeout)
        # 属主线程 = 将要运行 loop.run() 的线程。默认进程主线程，start() 会用
        # 真实线程覆盖（headless 循环不保证跑在主线程）。
        self._owner = owner or threading.main_thread()
        self._lock = threading.Lock()
        self._pending: set[Future[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"MainLoopService(loop={type(self._loop).__name__}, "
            f"closed={self._closed}, timeout={self._timeout})"
        )
        if verbose:
            base = base[:-1] + (
                f", owner={self._owner.name!r}, pending={len(self._pending)})"
            )
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """记录属主线程（在 ``Application.on_loop`` 里调用，幂等）。

        该 hook 与 ``loop.run()`` 在**同一线程**、紧邻其前执行，因此这里
        捕获的线程就是主循环属主。
        """
        self._owner = threading.current_thread()
        _logger.debug(
            "MainLoopService bound to thread %r (loop=%s)",
            self._owner.name, type(self._loop).__name__,
        )

    def close(self) -> None:
        """停止受理并取消在途调用（幂等）。

        被取消的调用方会立即收到 :class:`MainLoopUnavailable`，不必等超时；
        已经排在循环队列里但尚未执行的回调会看到"已取消"的 Future 并放弃，
        不会在拆机过程中再去碰已关闭的服务。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending)
            self._pending.clear()
        for future in pending:
            future.cancel()
        _logger.debug(
            "MainLoopService closed (%d pending cancelled)", len(pending),
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def loop(self) -> Any:
        return self._loop

    @property
    def timeout(self) -> float:
        return self._timeout

    def owner_thread(self) -> threading.Thread:
        return self._owner

    def in_main_thread(self) -> bool:
        """当前线程是否就是主循环属主线程。"""
        return threading.current_thread() is self._owner

    # ------------------------------------------------------------------ 调度
    def call(self, fn: Callable[[], T]) -> T:
        """在属主线程执行 ``fn`` 并同步返回结果。

        Raises:
            MainLoopUnavailable: 已 ``close()``，或无法把工作投递到主循环。
            MainThreadTimeout: ``timeout`` 内主循环没有响应（大概率被阻塞）。

        注意（迟到执行）：超时 / 关闭后，排队中的 ``fn`` **仍可能迟到执行**
        —— ``Future.cancel()`` 只能阻止"尚未开始"的回调。因此 ``fn`` 必须是
        "结果被丢弃也能安全结束"的操作；这与旧门面 ``Dispatcher.call`` 的取舍
        一致（那份实现已随 ``service/old/command.py`` 删除）。
        """
        if self._closed:
            raise MainLoopUnavailable("main loop service is shutting down")

        # 1) 已在属主线程：直接执行。既是快路径，也是自锁保护。
        if self.in_main_thread():
            return fn()

        # 2) 没有可用主循环：退化为同步执行（单测替身 / 未装配）。
        loop = self._loop
        if loop is None or not callable(getattr(loop, "call_soon", None)):
            _logger.debug(
                "MainLoopService: no usable main loop (%s); running inline",
                None if loop is None else type(loop).__name__,
            )
            return fn()

        # 3) 投递到主循环并等待。
        future: Future[T] = Future()

        def _run() -> None:
            # False = 调用方已超时 / 已关闭：丢弃这次执行。
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 —— 原样带回调用线程
                future.set_exception(exc)

        with self._lock:
            if self._closed:
                raise MainLoopUnavailable("main loop service is shutting down")
            self._pending.add(future)

        try:
            try:
                loop.call_soon(_run)
            except NotImplementedError:
                # 只实现 run/quit 的替身循环（``MainLoop`` 基类的 call_soon
                # 就是抛 NotImplementedError）：退化为同步执行，与"没有主
                # 循环"同义。这类循环在单测里很常见，不该让装配期炸。
                _logger.debug(
                    "MainLoopService: %s does not implement call_soon(); "
                    "running inline", type(loop).__name__,
                )
                return fn()
            except Exception as exc:  # noqa: BLE001 —— 循环侧不可达
                raise MainLoopUnavailable(
                    f"cannot reach main loop: {exc}"
                ) from exc

            try:
                return future.result(timeout=self._timeout)
            except FutureTimeout as exc:
                future.cancel()
                raise MainThreadTimeout(
                    self._timeout,
                    f"main loop gave no response within {self._timeout}s; "
                    f"it may be blocked",
                ) from exc
            except CancelledError:
                raise MainLoopUnavailable(
                    "main loop service is shutting down"
                ) from None
        finally:
            with self._lock:
                self._pending.discard(future)
