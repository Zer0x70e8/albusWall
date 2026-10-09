#
"""Template for a new presenter. Delete this line after copying."""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

if TYPE_CHECKING:
    from albuswall.core import Container
    from ..window.xxx import XxxView

_logger = getLogger(__name__)


class XxxPresenter(QObject):
    def __init__(self, target: "XxxView", parent=None) -> None:
        super().__init__(parent)
        self._target = target

    def setup(self, container: "Container") -> None:
        config = container.require("config")
        # 1. 样式
        # 2. 接线
        self._wire()

    def teardown(self) -> None:
        pass

    def _wire(self) -> None:
        w = self._target
        w.close_button.clicked.connect(w.close_requested.emit)
        # 一条一条连，不要 try/except
