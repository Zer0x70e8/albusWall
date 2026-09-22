#
""""""

from __future__ import annotations

import logging
import signal
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class MainLoop(ABC):
    """主循环抽象：run() 阻塞直到退出；quit() 请求退出。"""
    exit_code: int = -1
    code = exit_code

    @abstractmethod
    def run(self) -> int: ...

    @abstractmethod
    def quit(self) -> None: ...


# noinspection unused-parameter
class HeadlessMainLoop(MainLoop):
    """无 UI：Event.wait() 阻塞主线程，0 CPU 占用。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._signals_installed = False

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

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        logger.debug("HeadlessMainLoop received signal %s", signum)
        self._stop.set()

    def run(self) -> int:
        self._install_signals()
        logger.debug("HeadlessMainLoop entering wait().")
        self._stop.wait()
        logger.debug("HeadlessMainLoop exited.")
        return 0

    def quit(self) -> None:
        self._stop.set()
