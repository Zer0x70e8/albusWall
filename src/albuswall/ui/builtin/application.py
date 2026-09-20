#
""""""

from __future__ import annotations

import sys
from logging import getLogger
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from albuswall.core import MainLoop
from albuswall.log import TRACE, Logger

from ..protocol import register_ui
from .window import Window
from .presenter import (
    PresenterManager, WindowPresenter,
    AlbumPresenter)

if TYPE_CHECKING:
    from albuswall.core import Container

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


@register_ui("builtin")
class Application(QApplication):
    presenters: PresenterManager

    class MainLoop(MainLoop):
        def __init__(self, parent: "Application"):
            self.parent = parent

        def run(self) -> int:
            return self.parent.exec()

        def quit(self) -> None:
            self.parent.quit()

    def __init__(self):
        super().__init__(sys.argv)
        # 加一个空 QTimer 让解释器有机会处理信号
        self._signal_timer = QTimer()
        self.main_loop = Application.MainLoop(self)
        self.window = None

    # noinspection unused-parameter
    def setup(self, container: "Container") -> None:
        self.window = Window()
        self.presenters = PresenterManager()

        self.presenters.add(WindowPresenter(self.window, self.window))
        self.presenters.add(
            AlbumPresenter(self.window.title_bar, container.get("view_service")),
        )

        from .config.setup import setup  # lazy load
        setup(container.get("config"))

        self.presenters.setup(container)

        if container.get("config").static.debug:
            QTimer.singleShot(10, lambda: _logger.debug(str(self)))

    def teardown(self) -> None:
        self.presenters.teardown()

    # noinspection method-overriding
    def exec(self) -> int:
        self._signal_timer.timeout.connect(lambda: None)
        self._signal_timer.start(100)
        self.window.show()
        return super().exec()

    def __str__(self):
        return "\n".join((
            f"{type(self).__name__}(",
            f"\tmainWindow: {self.window}",
            f"{self.presenters.format(index=1)}",
            ")",
        ))


UIApplication = Application
