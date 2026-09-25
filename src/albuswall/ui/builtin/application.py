#
""""""

from __future__ import annotations

import sys
import traceback
from logging import getLogger
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, QEvent, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from albuswall.core import MainLoop
from albuswall.log import TRACE, Logger

from ..protocol import register_ui
from .window import Window
from .presenter import (
    PresenterManager, WindowPresenter,
    AlbumPresenter, ThumbnailGridPresenter,
    IngestSourcePresenter
)

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
        self.window = None

    # noinspection unused-parameter
    def setup(self, container: "Container") -> None:
        if container.get("config").static.debug:
            self._debug = True

            # noinspection unused-parameter
            def handler(mode, ctx, msg):
                if "parent hierarchy" in msg:
                    traceback.print_stack()

            qInstallMessageHandler(handler)

        self.window = Window()
        self.presenters = PresenterManager()

        window_presenter = WindowPresenter(self.window, self.window)
        album_presenter = AlbumPresenter(self.window.title_bar,
                                         container.get("view_service"))
        thumb_presenter = ThumbnailGridPresenter(
            self.window.content,
            thumb_repo=container.get("thumbnail_repo"),
            thumb_service=container.get("thumbnail_service"),
            spec="medium",
        )
        self.presenters.add(window_presenter)
        self.presenters.add(album_presenter)
        self.presenters.add(thumb_presenter)
        self.presenters.add(IngestSourcePresenter(
            self.window.source, container.get("source_service"),
        ))

        # 缩略图被激活 → 交给 window_presenter 去取整图、写 detail
        thumb_presenter.item_activated.connect(window_presenter.show_detail)

        # 建立数据流：相册变化 → 网格刷新
        def _on_album_changed(album):
            try:
                asset_ids = container.get("view_service").get_asset_ids(album)
            except Exception as exc:
                _logger.error("list asset ids failed for album %s: %s", album, exc)
                asset_ids = []
            thumb_presenter.set_assets(asset_ids)

        album_presenter.album_changed.connect(_on_album_changed)

        from .config.setup import setup  # lazy load
        setup(container.get("config"))

        self.presenters.setup(container)

        if container.get("config").static.debug:
            QTimer.singleShot(10, lambda: _logger.debug(str(self)))

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


UIApplication = Application
