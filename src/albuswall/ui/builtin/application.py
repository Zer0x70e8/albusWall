#
""""""

from __future__ import annotations

import sys
import traceback
from logging import getLogger
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QEvent, qInstallMessageHandler

from albuswall.core import MainLoop
from albuswall.log import TRACE, Logger

from ..protocol import register_ui
from .window import Window
from .presenter import PresenterManager

if TYPE_CHECKING:
    from albuswall.core import Container

NOISY = {
    QEvent.Type.MouseMove,
    QEvent.Type.Paint,
    QEvent.Type.UpdateRequest,
    QEvent.Type.Timer,
    QEvent.Type.MetaCall,
    QEvent.Type.ChildPolished,
    QEvent.Type.PolishRequest,
    QEvent.Type.StyleChange,
    QEvent.Type.LayoutRequest,
    QEvent.Type.HoverMove,
    QEvent.Type.Enter,
    QEvent.Type.Leave,
}

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


@register_ui("builtin")
class Application(QApplication):
    # 显式声明（可选，仅为了类型提示友好）；实例属性在 __init__ 里赋初值
    window: Optional["Window"]
    presenters: Optional["PresenterManager"]

    class MainLoop(MainLoop):
        def __init__(self, parent: "Application"):
            self.parent = parent

        def run(self) -> int:
            return self.parent.exec()

        def quit(self) -> None:
            self.parent.quit()

    def __init__(self):
        super().__init__(sys.argv)
        # 关键：提前把生命周期字段初始化为 None，
        # 这样即使 setup() 半路崩溃，teardown()/__str__/__repr__ 也安全
        self.window: Optional[Window] = None
        self.presenters: Optional[PresenterManager] = None

        self._signal_timer = QTimer()
        self._debug = False
        self.main_loop = Application.MainLoop(self)

    # noinspection unused-parameter
    def setup(self, container: "Container") -> None:
        config = container.get("config")  # ← key 从 _config 改成 config

        if config.static.debug:
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

        # 3. 配置文件里的 UI 相关 setup（key 同样修正）
        try:
            from .config.setup import setup as setup_ui_config  # lazy load
            setup_ui_config(config)
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

    # noinspection method-overriding
    def exec(self) -> int:
        self._signal_timer.timeout.connect(lambda: None)
        self._signal_timer.start(100)
        if self.window is not None:
            self.window.show()
        return super().exec()

    # def notify(self, receiver, event, /) -> bool: ...

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
