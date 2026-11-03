#
"""PySide6-backed main loop: 一个**被动**的 ``QApplication``。

本模块只回答「事件循环怎么跑」：创建 ``QApplication``、跑 ``exec()``、把退出码
带出来、线程安全地把回调排回 GUI 线程、把进程信号映射成退出码。

界面装配（语言、主窗口、presenter、容器键）**不在这里**——那是 boot 层
(:mod:`albuswall.ui.bootstrap`) 的事，由 ``albuswall.plugins.builtin_ui`` 在
启动流程里调用一次（对位 ``main.py::_boot`` 的 phase 4）。

被动的两条直接后果：

* 本类**不注册任何 hook**。插件不再需要「谁先谁后」的隐式约定：窗口在
  ``main_loop`` 槽位工厂里就绪，早于 ``exec()`` 的任何 boot / init hook。
* 本类**不持有** ``main_window`` / ``presenter`` / ``translations``。要窗口就
  ``container.get("main_window")``，要 presenter 就
  ``container.get("presenter")``——应用对象不再兼任装配中心。

换主循环 = 换掉这个类；只换界面内容 = 不碰它，改容器里的 ``main_window`` /
``presenter``。
"""

from __future__ import annotations

import signal
import sys
import threading
import traceback
from collections.abc import Callable, Sequence
from types import FrameType

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from albuswall.log import getLogger
from albuswall.core import ExitCode, MainLoop

__all__ = ["Application", "UIApplication"]

logger = getLogger(__package__)

_Soon = Callable[[], None]

#: Qt 会阻塞解释器；此周期让 Python 信号处理器有机会运行。
_SIGNAL_POLL_MS = 200

#: 需要映射为退出码的进程信号。
_QUIT_SIGNALS: tuple[tuple[int, int], ...] = (
    (signal.SIGINT, 130),
    (signal.SIGTERM, 143),
)


class _CallSoonBridge(QObject):
    """``call_soon`` 的线程安全桥：emit → QueuedConnection → GUI 线程。"""

    _posted = Signal(object)

    def __init__(self, app: "Application") -> None:
        super().__init__(app)
        self._app = app
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn: _Soon) -> None:
        try:
            fn()
        except SystemExit as exc:
            # 兼容 ``fn`` 内部 raise SystemExit(code) 的写法。
            self._app.quit(exc.code if isinstance(exc.code, int) else 0)
        except Exception:  # noqa: BLE001 - 不能因为单个回调崩溃整个 GUI
            traceback.print_exc()

    def post(self, fn: _Soon) -> None:
        self._posted.emit(fn)


class Application(QApplication, MainLoop):
    """被动 Qt 主循环。同时是 ``QApplication`` 与 ``MainLoop``。

    生命周期只有三段（全在 GUI 线程）：

    * ``__init__`` —— 创建 ``QApplication``、装信号处理器，**仅此而已**；
    * ``run()``    —— ``exec()``，返回 Qt 的退出码（由 ``MainLoop.run`` 驱动）；
    * ``quit()``   —— 请求退出；``call_soon`` 线程安全地把回调排回 GUI 线程。

    窗口、语言、presenter 全由 boot 层装配（见 :mod:`albuswall.ui.bootstrap`），
    所以本类可以单独用于「只想跑一个 Qt 事件循环」的宿主，也方便被
    ``set_slot("main_loop", ...)`` 整个替换掉。
    """

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        QApplication.__init__(self, list(argv) if argv else sys.argv)
        self.code: int = 0
        self._bridge = _CallSoonBridge(self)
        self._sig_timer: QTimer | None = None
        self._install_signals()

    # ---------------- MainLoop 协议 ----------------
    def run(self) -> int | ExitCode:
        logger.debug("ui.qt: Qt event loop starting")
        code = self.exec()
        logger.debug("ui.qt: Qt event loop stopped (code=%s)", code)
        return code

    # noinspection method-overriding
    def quit(self, code: int | ExitCode | None = None) -> None:
        if code is not None:
            self.code = int(code)
        self.exit(self.code)  # 线程安全

    def call_soon(self, fn: _Soon) -> None:
        self._bridge.post(fn)

    # ---------------- 信号 ----------------
    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, code in _QUIT_SIGNALS:
            try:
                signal.signal(sig, self._make_signal_handler(code))
            except (ValueError, OSError):
                logger.debug("ui.qt: cannot install handler for %s", sig)
        # Qt 的 C++ 事件循环阻塞解释器；周期 tick 让信号处理器有机会执行。
        self._sig_timer = QTimer(self)
        self._sig_timer.timeout.connect(self._tick)
        self._sig_timer.start(_SIGNAL_POLL_MS)

    @staticmethod
    def _make_signal_handler(code: int) -> Callable[[int, FrameType | None], None]:
        def _handler(signum: int, _frame: FrameType | None) -> None:
            app = QApplication.instance()
            if isinstance(app, Application):
                app.quit(code)
            else:
                raise SystemExit(128 + signum)

        return _handler

    @staticmethod
    def _tick() -> None:
        """空操作；timer 存在让解释器能跑信号处理器。"""


UIApplication = Application
