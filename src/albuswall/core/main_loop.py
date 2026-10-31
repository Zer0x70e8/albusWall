#
""""""

from __future__ import annotations

import logging
import queue
import signal
import threading
import traceback
from collections.abc import Callable

from .exit_codes import ExitCode

logger = logging.getLogger(__package__)

_Soon = Callable[[], None]


class MainLoop:
    """主循环接口：run() 阻塞直到退出；quit() 请求退出。

    （其余 docstring 保持）

    注意：本类**不是** ABC。UI 层 ``class Application(QApplication, MainLoop)``
    需要同时继承 PySide6 的 ``Shiboken.ObjectType`` 元类；若 ``MainLoop`` 用
    ``ABCMeta``，Python 找不到同时是两者子类的元类，会抛
    ``TypeError: metaclass conflict``。

    未实现的子类方法在运行期以 ``NotImplementedError`` 暴露。这与原有的
    ``call_soon`` 默认实现是同一策略。
    """

    code: int = -1

    def run(self) -> int | ExitCode:
        raise NotImplementedError(
            f"{type(self).__name__}.run() is not implemented")

    def quit(self, code: int | ExitCode | None = None) -> None:
        raise NotImplementedError(
            f"{type(self).__name__}.quit() is not implemented")

    def call_soon(self, fn: _Soon) -> None:
        """线程安全：把 ``fn`` 排到主循环所属线程执行。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not support call_soon(); "
            f"cross-thread command dispatch is unavailable.")


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
        self.code: int = 0

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

    # noinspection PyBroadException
    def run(self) -> int:
        self._install_signals()
        logger.debug("HeadlessMainLoop entering wait().")
        while not self._stop.is_set():
            try:
                fn = self._calls.get(timeout=self._IDLE_POLL)
            except queue.Empty:
                continue
            try:
                fn()
            except SystemExit as e:
                # 回调里 raise SystemExit 等价于请求退出；转成正常的 quit(code)
                code = e.code if isinstance(e.code, int) else 0
                self.quit(code)
            except Exception:
                traceback.print_exc()
        logger.debug("HeadlessMainLoop exited.")
        # 唯一真值来源：Runtime 设置过就是 Runtime 的，否则是信号/默认 0
        return self.code

    def quit(self, code: int | None = None) -> None:
        if code is not None:
            self.code = code
        self._stop.set()
