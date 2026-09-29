#
""""""

from __future__ import annotations

import sys
import traceback
from logging import getLogger
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import (
    QTimer, QEvent, qInstallMessageHandler
)

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
    presenters: PresenterManager
    window: Window

    class MainLoop(MainLoop):
        def __init__(self, parent: "Application"):
            self.parent = parent
            self.presenters = None

        def run(self) -> int:
            return self.parent.exec()

        def quit(self) -> None:
            self.parent.quit()

    def __init__(self):
        super().__init__(sys.argv)
        # 加一个空 QTimer 让解释器有机会处理信号
        self._signal_timer = QTimer()
        self._debug = False
        self.main_loop = Application.MainLoop(self)

    # noinspection unused-parameter
    def setup(self, container: "Container") -> None:
        if container.get("config").static.debug:
            self._debug = True

            # noinspection unused-parameter
            def handler(mode, ctx, msg):
                if "parent hierarchy" in msg:
                    traceback.print_stack()

            qInstallMessageHandler(handler)
            QTimer.singleShot(10, lambda: _logger.debug(repr(self)))

        self.window = Window()
        self.presenters = PresenterManager(self.window, container).build()

        from .config.setup import setup  # lazy load
        setup(container.get("config"))

        self.presenters.setup(container)

    def teardown(self) -> None:
        if self.presenters is not None:
            self.presenters.teardown()

    # noinspection method-overriding
    def exec(self) -> int:
        self._signal_timer.timeout.connect(lambda: None)
        self._signal_timer.start(100)
        self.window.show()
        return super().exec()

    # def notify(self, receiver, event, /) -> bool:
    #
    #     if event.type() not in NOISY:
    #         print(f"[notify] {event.type().name:24s} -> "
    #               f"{type(receiver).__name__}({receiver.objectName()})")
    #     return super().notify(receiver, event)

    def __str__(self):
        return "\n".join((
            f"{type(self).__name__}(",
            f"\tmainWindow: {self.window}",
            f"{self.presenters.format(index=1)}",
            ")",
        ))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            # f"debug={self._debug!r}, "
            f"mainWindow={self.window.objectName() 
            if self.window is not None else 'None'}, "
            f"presenters={len(self.presenters)})"
        )

UIApplication = Application
