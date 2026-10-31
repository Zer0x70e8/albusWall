#
"""PySide6-backed MainLoop and default Application."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Sequence
from types import FrameType

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from albuswall import __title__
from albuswall.configue import configclass
from albuswall.core.container import Container
from albuswall.core.exit_codes import ExitCode
from albuswall.core.main_loop import MainLoop
from albuswall.core.registry import hooks_map

from .i18n import TranslationManager
from .languages import DEFAULT_LANGUAGE
from .window import Window

__all__ = ["Application", "UIApplication"]

logger = logging.getLogger(__package__)

_Soon = Callable[[], None]

#: 默认窗口标题与尺寸。
DEFAULT_TITLE = __title__
DEFAULT_SIZE = (960, 720)

#: 无 ``ui_root_factory`` 时的占位文案与样式。
_PLACEHOLDER = "AlbusWall is running.\nNo UI plugin provided content."
_PLACEHOLDER_STYLE = "padding: 24px; font-size: 14px;"

#: Qt 会阻塞解释器；此周期让 Python 信号处理器有机会运行。
_SIGNAL_POLL_MS = 200

#: 需要映射为退出码的进程信号。
_QUIT_SIGNALS: tuple[tuple[int, int], ...] = (
    (signal.SIGINT, 130),
    (signal.SIGTERM, 143),
)


# noinspection bad-assignment
@configclass("ui", "main")
class UIConf:
    """``[ui]`` 段：界面偏好（当前只有启动语言）。

    ``albuswall.ui`` 是在插件发现阶段（``setup_config`` + ``load()`` 之后）
    才被 import 的，装饰晚于 ``load()`` 的 Step 4；``configclass`` 对
    「``Configue`` 已构造」的类会在装饰当下立即注入类型与默认值
    （``inject_raw(overwrite=False)``，文件里写了的值照样赢），所以这里可以
    直接写默认值。

    声明本身的价值：``(section, key)`` 进了声明表，``load(strict_undeclared
    =True)`` 不会把它当幽灵 key，拼错的 ``[ui] languge`` 也读不到值。
    """

    language: str = DEFAULT_LANGUAGE


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
    """默认 Qt 应用。同时是 ``QApplication`` 与 ``MainLoop``。

    生命周期（全在 GUI 线程）：

    * ``__init__``           —— 创建 QApplication，``ui_app`` 进容器
    * ``setup(container)``   —— 构建主窗口；可 override 或用 ``ui_root_factory``
    * ``show(container)``    —— 显示主窗口
    * ``exec()``             —— Qt 事件循环（由 ``MainLoop.run`` 驱动）

    **hook 插到列表头部**（``init`` / ``loop``）：保证 ``setup`` / ``show``
    先于其他插件的同名 hook 执行，插件在 ``on_init`` / ``on_loop`` 里可以
    直接访问 ``ui_app.main_window``。
    """

    main_window: Window

    #: 界面语言管理器（与容器里的 ``i18n`` 是同一个对象）。
    translations: TranslationManager

    def __init__(
            self,
            container: Container,
            argv: Sequence[str] | None = None,
    ) -> None:
        QApplication.__init__(self, list(argv) if argv else sys.argv)
        self._container = container
        self.code: int = 0
        self._setup_done = False
        self._show_done = False
        self._bridge = _CallSoonBridge(self)
        self._sig_timer: QTimer | None = None

        # ui_app 立即进容器：其他插件在 on_init 阶段就能拿到 UI 对象，
        # 只是此时 main_window 还没建（setup 尚未跑）。
        container.set("ui_app", self)

        # 语言管理器也尽早进容器：插件直接 ``container.get("i18n")`` 就能切语言。
        self.translations = TranslationManager(self)
        container.set("i18n", self.translations)

        # 插入头部而非 append：保证窗口在插件访问前已就绪。
        hooks_map["init"].insert(0, self._on_init_hook)
        hooks_map["loop"].insert(0, self._on_loop_hook)

        self._install_signals()

    # ---------------- 子类扩展点 ----------------
    def setup(self, container: Container) -> None:
        """构建主窗口。默认行为：QMainWindow + 中心控件。

        中心控件来源（优先级）：
        1. ``container.get("ui_root_factory")`` 的返回值
        2. 占位 QLabel

        要完全自定义窗口，override 本方法；只换内容，注册 ``ui_root_factory``。
        """
        # 先定语言再建窗口：控件一出生就是目标语言的文案
        self.set_language(self._configured_language())

        window = Window()
        window.setWindowTitle(DEFAULT_TITLE)
        window.resize(*DEFAULT_SIZE)

        self.main_window = window
        logger.debug("ui.qt: main window built")

    # ---------------- 语言 ----------------
    def set_language(self, language: str) -> bool:
        """切换界面语言；参数直接给字面量（``"zh_CN"`` / ``"en_US"`` / ``"en"``）。

        等价于 ``container.get("i18n").set_language(...)``，也等价于模块级
        ``albuswall.ui.i18n.set_language(...)``。
        """
        return self.translations.set_language(language)

    @staticmethod
    def _configured_language() -> str:
        """读 ``[ui] language`` 作为启动语言；空白 / 缺值时用默认语言。

        用 :class:`UIConf` 类视图而不是 ``config.tree``：key 在声明期就注册，
        默认值由 ``configclass`` 注入 Resolver。``getattr`` 只是防御性兜底
        （``ConfigMissingError`` 继承 ``AttributeError``）：单测里 UI 可能被
        单独加载，此时没有 ``Configue`` 单例。
        """
        value = getattr(UIConf(), "language", DEFAULT_LANGUAGE)
        text = str(value or "").strip()
        return text or DEFAULT_LANGUAGE

    def show(self, container: Container) -> None:
        """显示主窗口。"""
        if self.main_window is not None:
            self.main_window.show()
            logger.debug("ui.qt: main window shown")

    # ---------------- hook 桥接 ----------------
    def _on_init_hook(self) -> None:
        self._ensure_setup()

    def _on_loop_hook(self, _: object) -> None:
        self._ensure_show()

    def _ensure_setup(self) -> None:
        if self._setup_done:
            return
        self._setup_done = True
        self.setup(self._container)

    def _ensure_show(self) -> None:
        if self._show_done:
            return
        self._show_done = True
        self.show(self._container)

    # ---------------- MainLoop 协议 ----------------
    def run(self) -> int | ExitCode:
        # 兜底：Application 若被延迟构造（槽位 resolve），init 阶段已过，
        # 此处补调；正常路径下两者都在 hook 里跑过了（幂等）。
        self._ensure_setup()
        self._ensure_show()
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
