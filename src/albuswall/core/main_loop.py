#
""""""

from __future__ import annotations

import logging
import signal
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class MainLoop(ABC):
    """主循环抽象：run() 阻塞直到退出；quit() 请求退出。

    `code` 是主循环退出码：run() 返回它。Runtime 在 quit() 前
    可以覆写它来指定退出码（例如异常 → 1、SIGINT → 130）。
    """
    code: int | str = -1

    @abstractmethod
    def run(self) -> int | str: ...

    @abstractmethod
    def quit(self, code: int | str | None = None) -> None: ...


class HeadlessMainLoop(MainLoop):
    """无 UI：Event.wait() 阻塞主线程，0 CPU 占用。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
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

    def run(self) -> int | str:
        self._install_signals()
        logger.debug("HeadlessMainLoop entering wait().")
        self._stop.wait()
        logger.debug("HeadlessMainLoop exited.")
        # 唯一真值来源：Runtime 设置过就是 Runtime 的，否则是信号/默认 0
        return self.code

    def quit(self, code: int | str | None = None) -> None:
        if code is not None:
            self.code = code
        self._stop.set()
