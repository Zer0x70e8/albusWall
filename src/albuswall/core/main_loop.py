#
""""""

from __future__ import annotations

import logging
import queue
import signal
import threading
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable

logger = logging.getLogger(__name__)

_Soon = Callable[[], None]


class MainLoop(ABC):
    """主循环抽象：run() 阻塞直到退出；quit() 请求退出。

    `code` 是主循环退出码：run() 返回它。Runtime 在 quit() 前
    可以覆写它来指定退出码（例如异常 → 1、SIGINT → 130）。

    `call_soon` 是「从别的线程投递回调到主循环线程」的唯一入口。进程外
    的调用方（IPC / CLI）拿到请求后，必须经它回到属主线程再碰服务状态，
    否则会违反 state 的单线程属主约定。

    默认实现抛 NotImplementedError，因为第三方自定义 MainLoop 可能在
    本方法出现之前就已经存在：缺了它只应让 IPC 这类可选能力降级，
    而不该让整个应用起不来。内置的两个实现都已覆盖。
    """

    code: int | str = -1

    @abstractmethod
    def run(self) -> int | str: ...

    @abstractmethod
    def quit(self, code: int | str | None = None) -> None: ...

    def call_soon(self, fn: _Soon) -> None:
        """线程安全：把 ``fn`` 排到主循环所属线程执行。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not support call_soon(); "
            f"cross-thread command dispatch is unavailable."
        )


class HeadlessMainLoop(MainLoop):
    """无 UI：阻塞在一个「回调队列 + 退出事件」的消费循环里，空闲时 0 CPU。

    与原先的 ``Event.wait()`` 相比，区别只在于多消费一个队列：
    ``call_soon`` 排进来的回调在**本线程**执行，满足「主循环线程是
    state 属主」的约定。
    """

    #: 空闲时每次等待的超时（秒）。只影响退出响应速度，不影响 CPU 占用。
    _IDLE_POLL = 0.1

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._calls: queue.Queue[_Soon] = queue.Queue()
        self._signals_installed = False
        # 实例级 code，覆盖基类默认 -1；正常退出为 0
        self.code: int | str = 0

    def _install_signals(self) -> None:
        if self._signals_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        self._signals_installed = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass

    def _on_signal(self, signum, frame) -> None:
        logger.debug("HeadlessMainLoop received signal %s", signum)
        # 128 + signum 是 Unix shell 约定：SIGINT=2 -> 130, SIGTERM=15 -> 143
        self.code = 128 + signum
        self._stop.set()

    # ---------- 跨线程投递 ----------
    def call_soon(self, fn: _Soon) -> None:
        self._calls.put(fn)

    def run(self) -> int | str:
        self._install_signals()
        logger.debug("HeadlessMainLoop entering wait().")
        while not self._stop.is_set():
            try:
                fn = self._calls.get(timeout=self._IDLE_POLL)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception:  # noinspection PyBroadException
                traceback.print_exc()
        logger.debug("HeadlessMainLoop exited.")
        # 唯一真值来源：Runtime 设置过就是 Runtime 的，否则是信号/默认 0
        return self.code

    def quit(self, code: int | str | None = None) -> None:
        if code is not None:
            self.code = code
        self._stop.set()
