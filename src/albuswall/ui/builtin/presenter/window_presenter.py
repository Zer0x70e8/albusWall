#
""""""

from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, QTimer

from ..widgets import blur_overlay_label
from ..config import WindowPresenterConfs
from .window_resizer import WindowResizer

if TYPE_CHECKING:
    from albuswall.core import Container
    from albuswall.configue import Configue
    from albuswall.services import ViewService
    from ..window.window import Window

_logger = getLogger(__name__)


class Presenter(QObject):
    _service: "ViewService"

    def __init__(self, target: "Window", parent=None):
        super().__init__(parent)
        self._target = target
        self.confs = WindowPresenterConfs()
        self.window_resizer = WindowResizer(self._target, self._target)

    def setup(self, container: "Container"):
        config = container.get("config")

        self._service = container.get("view_service")

        self.confs.ensure_theme_files_completed(config)

        self._target.resize(800, 600)
        self._apply_style_sheet(config)

        # noinspection none-function-assignment
        QTimer.singleShot(0, lambda: (setattr(
            self.window_resizer, "title_bar_height",
            self._target.title_bar.action_bar_bottom_y_in_parent)))

        # connect
        self._connector(
            self._target.title_bar.album_button.clicked,
            lambda: self._target.overlay.setCurrentWidget(
                self._target.album
            )
        )
        self._connector(
            self._target.album.close_button.clicked,
            self._target.overlay.show_blank
        )
        # self._connector(
        #     self._target.menu.
        # )

    def _apply_style_sheet(self, config: "Configue") -> None:
        """解析 QSS 路径并应用到目标 widget。

        路径解析优先走 WindowPresenterConfs._resolve_style_sheet，
        它会依次尝试：
            1. ui.style_sheet（显式指定）
            2. path.theme / ui.theme / files.qss（回退）
        """
        qss: Optional[Path] = self.confs.resolve_style_sheet(config)

        if qss is None:
            _logger.info(
                "No QSS path resolved (ui.style_sheet is unset and "
                "path.theme is missing); skip stylesheet."
            )
            return

        if not qss.is_file():
            _logger.warning("QSS file is not available: %s", qss)
            return

        try:
            self._target.setStyleSheet(qss.read_text(encoding="utf-8"))
            blur_overlay_label.DRAW_LABEL_CONTENT = False
        except OSError as exc:
            _logger.warning("Failed to read QSS %s: %s", qss, exc)
            return

        _logger.debug("Loaded QSS from %s", qss)

    @staticmethod
    def _connector(signal, slot):
        try:
            signal.connect(slot)
        except Exception as e:
            _logger.error(f"Slot({slot}) connect Signal({signal}) "
                          f"failed as %s", e)


WindowPresenter = Presenter
