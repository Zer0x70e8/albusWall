#
""""""

from __future__ import annotations

import sys
import traceback
from logging import getLogger
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QEvent,
    QObject,
    QTimer,
    Signal,
    qInstallMessageHandler,
)
from PySide6.QtWidgets import QApplication

from albuswall.core import MainLoop
from albuswall.log import TRACE, Logger

from ..protocol import register_ui
from .presenter import PresenterManager
from .window import Window

if TYPE_CHECKING:
    from albuswall.core import Container

_QET = QEvent.Type
NOISY = {
    # ---- 高频噪声 ----
    _QET.MouseMove,
    _QET.HoverMove,
    _QET.Enter,
    _QET.Leave,
    _QET.Paint,
    _QET.UpdateRequest,
    _QET.UpdateLater,
    _QET.Timer,
    _QET.MetaCall,
    _QET.StyleChange,
    _QET.LayoutRequest,
    _QET.PolishRequest,
    _QET.ChildPolished,
    _QET.ChildAdded,
    _QET.ChildRemoved,  # 除非你在查 setParent 时才打开
    _QET.DynamicPropertyChange,
    _QET.ContentsRectChange,
    _QET.WindowTitleChange,
    _QET.ScreenChangeInternal,

    # ---- 光标的"移动即发" ----
    _QET.CursorChange,
    _QET.HoverEnter,
    _QET.HoverLeave,

    # ---- 布局类，凡是自动 layout 都会发 ----
    _QET.Move,  # ← 滚动区域不停发
    _QET.Resize,  # ← 同上
    _QET.Show,  # 加进来前先想清楚：查显示时机时才临时移除
    _QET.Hide,
    _QET.ShowToParent,
    _QET.HideToParent,
    _QET.Expose,

    # ---- 输入法 / 提示 ----
    _QET.InputMethodQuery,
    _QET.InputMethod,
    _QET.StatusTip,

    # ---- 底层平台 ----
    _QET.PlatformSurface,
    _QET.WinIdChange,
    _QET.WindowIconChange,

    # ---- 激活/焦点，一般也不用看 ----
    _QET.ApplicationActivate,
    _QET.ApplicationDeactivate,
    _QET.WindowActivate,
    _QET.WindowDeactivate,
    _QET.ActivationChange,
    _QET.FocusAboutToChange,
    _QET.FocusIn,
    _QET.FocusOut,
}

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


class _MainThreadBridge(QObject):
    """跨线程回调桥。

    Qt 的排队连接只需要一个「接收者拥有主线程亲和性」的 QObject：本对象
    在 ``Application.__init__`` 里创建（必然在主线程），因此从任意工作线程
    ``emit`` 都会走 ``Qt.AutoConnection`` → ``Qt.QueuedConnection``，
    ``_dispatch`` 在主线程事件循环里被调用。

    ``object`` 载荷用来搬运任意可调用对象；Qt 对 Python 对象做的是引用传递，
    不涉及 QVariant 转换。
    """

    _request = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._request.connect(self._dispatch)

    def _dispatch(self, fn) -> None:
        try:
            fn()
        except Exception:  # noinspection PyBroadException
            traceback.print_exc()

    def call_soon(self, fn) -> None:
        self._request.emit(fn)


# noinspection SpellCheckingInspection
@register_ui("builtin")
class Application(QApplication):
    # 显式声明（可选，仅为了类型提示友好）；实例属性在 __init__ 里赋初值
    window: Window | None
    presenters: PresenterManager | None

    class MainLoop(MainLoop):
        def __init__(self, parent: Application):
            self.parent = parent
            # 必须在主线程构造：桥的亲和性就是「主循环线程」的定义
            self._bridge = _MainThreadBridge()

        def run(self) -> int:
            return self.parent.exec()

        def quit(self, code=None) -> None:
            # 旧实现丢弃 code，导致 exec() 的返回值永远是 Qt 自己的退出码；
            # CLI 想让 GUI「以指定码退出」时会静默失效。
            if isinstance(code, str):
                code = 0 if code in ("0", "") else 1
            elif code is None:
                code = 0
            elif not isinstance(code, int):
                code = 1
            self.parent.exit(code)

        def call_soon(self, fn) -> None:
            self._bridge.call_soon(fn)

    def __init__(self):
        super().__init__(sys.argv)
        # 关键：提前把生命周期字段初始化为 None，
        # 这样即使 setup() 半路崩溃，teardown()/__str__/__repr__ 也安全
        self.window: Window | None = None
        self.presenters: PresenterManager | None = None

        self._signal_timer = QTimer()
        self._debug = False
        self.main_loop = Application.MainLoop(self)

        # 桌面集成：窗口图标 + .desktop 归属，装到系统后菜单里才有正确图标
        self._apply_desktop_identity()

    def _apply_desktop_identity(self) -> None:
        """设置窗口图标 / .desktop 文件名（AUR 等发行包依赖它）。

        两者都只是「锦上添花」：资源缺失时只记日志，绝不阻断启动。
        """
        try:
            # 让窗口管理器把窗口关联到 /usr/share/applications/albuswall.desktop
            self.setDesktopFileName("albuswall")
        except Exception as exc:
            _logger.debug("desktop file name not set: %s", exc)

        try:
            from PySide6.QtGui import QIcon

            from albuswall.resources import get

            icon = QIcon(str(get("app_icon")))
            if icon.isNull():
                _logger.debug("app icon could not be loaded, keep the default icon")
                return
            self.setWindowIcon(icon)
        except Exception as exc:
            _logger.debug("app icon setup skipped: %s", exc)

    # noinspection unused-parameter
    def setup(self, container: Container) -> None:
        # 配置统一走 typed dict（默认值来自 UI 静态声明）
        from ..bootstrap import ui_config

        confs = ui_config()

        # 0. 语言 / 翻译必须最先装好：后面所有控件的首次文案都直接取译文
        #    （没有 .qm 时退回源码原文，不影响运行）。
        self._install_translations()

        if confs["debug"]:
            self._debug = True

            # noinspection unused-parameter
            def handler(mode, ctx, msg):
                if "parent hierarchy" in msg:
                    traceback.print_stack()

            qInstallMessageHandler(handler)
            # 用 lambda 捕获 self，避免 debug 时 window/presenters 已就绪
            QTimer.singleShot(10, lambda: _logger.debug(repr(self)))

        # 1. 主窗口先建出来
        self.window = Window()

        # 2. PresenterManager 独立保护：即使 build 抛异常，
        #    主窗口仍然能显示，teardown 也安全
        try:
            # noinspection bad-argument-type
            self.presenters = PresenterManager(self.window, container).build()
        except Exception as exc:
            _logger.exception("PresenterManager build failed: %s", exc)
            self.presenters = None

        # 3. 配置文件里的 UI 相关 setup（preference.ini → dynamic.preference）
        try:
            from .config.setup import setup as setup_ui_config  # lazy load
            setup_ui_config()
        except Exception as exc:
            _logger.exception("UI config setup failed: %s", exc)

        # 4. presenter 逐个 setup（manager 内部已 try/except 隔离单个 presenter）
        if self.presenters is not None:
            self.presenters.setup(container)

    def teardown(self) -> None:
        if self.presenters is not None:
            try:
                self.presenters.teardown()
            except Exception as exc:
                _logger.exception("PresenterManager teardown failed: %s", exc)
            finally:
                self.presenters = None

    # ------------------------------------------------------------------ #
    # 语言 / 翻译
    # ------------------------------------------------------------------ #
    @staticmethod
    def _install_translations() -> None:
        """按配置装载翻译器（没有 .qm 时静默退化为源码原文）。

        失败只记日志：翻译属于「锦上添花」，不能挡住窗口启动。
        """
        try:
            from .i18n import translation_manager

            translation_manager().install()
        except Exception as exc:
            _logger.exception("translation install failed: %s", exc)

    # noinspection method-overriding
    def exec(self) -> int:
        self._signal_timer.timeout.connect(lambda: None)
        self._signal_timer.start(100)
        if self.window is not None:
            self.window.show()
        return super().exec()

    # def notify(self, receiver, event, /) -> bool:
    #
    #     if event.type() not in NOISY:
    #         print(f"[notify] {event.type().name:24s} -> "
    #               f"{type(receiver).__name__}({receiver.objectName()})")
    #     return super().notify(receiver, event)

    def __str__(self):
        window_repr = repr(self.window) if self.window is not None else "None"
        presenters_repr = (
            self.presenters.format(index=1)
            if self.presenters is not None else "None"
        )
        return "\n".join((
            f"{type(self).__name__}(",
            f"\tmainWindow: {window_repr}",
            f"\tpresenters: {presenters_repr}",
            ")",
        ))

    def __repr__(self) -> str:
        window_name = (
            self.window.objectName()
            if self.window is not None else "None"
        )
        n = len(self.presenters) if self.presenters is not None else 0
        return (
            f"{type(self).__name__}("
            f"mainWindow={window_name}, "
            f"presenters={n})"
        )


UIApplication = Application
