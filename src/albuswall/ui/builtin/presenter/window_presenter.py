#
""""""

from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QPixmap

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

    def __init__(
            self,
            target: "Window",
            parent=None
    ):
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
        # noinspection PyNoneFunctionAssignment
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
        self._connector(
            self._target.menu.source_action.triggered,
            lambda: self._target.overlay.setCurrentWidget(
                self._target.source
            )
        )
        self._connector(
            self._target.source.close_button.clicked,
            self._target.overlay.show_blank
        )

        # #
        self._connector(
            self._target.detail.close_button.clicked,
            self._target.overlay.show_blank,
        )

        self._connector(
            self._target.menu.setting_action.triggered,
            lambda: self._target.overlay.setCurrentWidget(
                self._target.setting
            )
        )
        self._connector(
            self._target.setting.close_button.clicked,
            self._target.overlay.show_blank
        )
        # # 菜单"设置" → 走 _open_setting（会先把浮动/关闭的 dock 收回，再切页）
        # self._connector(
        #     self._target.menu.setting_action.triggered,
        #     self._open_setting,
        # )
        #
        # # dock 被用户关闭 → overlay 切空白
        # self._connector(
        #     self._target.setting_station.widget_closed,
        #     self._target.overlay.show_blank,
        # )
        #
        # # dock 被拖出成独立窗口 → overlay 切空白
        # self._connector(
        #     self._target.setting_station.dock_floated,
        #     self._target.overlay.show_blank,
        # )

        # 拖回停靠：不自动切页（保持用户当前的页）
        # self._connector(
        #     self._target.setting_station.dock_docked,
        #     lambda: self._target.overlay.setCurrentWidget(self._target.setting_station),
        # )

    def _apply_style_sheet(self, _: "Configue") -> None:
        """解析 QSS 路径并应用到目标 window。"""
        qss: Optional[Path | str] = self.confs.qss

        if qss is None:
            _logger.info(
                "No QSS path resolved (ui.style_sheet is unset and "
                "path.theme is missing); skip stylesheet."
            )
            return
        qss: Path = Path(qss)

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

    # def _open_setting(self) -> None:
    #     """每次打开设置页：先把浮动/关闭的 dock 收回、恢复，再切页。"""
    #     station = self._target.setting_station
    #     dock = station.dock
    #
    #     # 关掉过 → 恢复显示
    #     station.show_dock()
    #
    #     # 浮动过 → 收回停靠（会顺带触发 dock_docked，无害）
    #     if dock is not None and dock.isFloating():
    #         dock.setFloating(False)
    #
    #     self._target.overlay.setCurrentWidget(station)

    def show_detail(self, asset_id: int) -> None:
        path = self._service.get_asset_full_path_by_id(asset_id)
        if path is None or not path.is_file():
            _logger.warning("detail: no full path for asset=%d", asset_id)
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            _logger.warning("detail: QPixmap load failed: %s", path)
            return

        self._target.overlay.setCurrentWidget(self._target.detail)
        self._target.detail.set_image(str(path))


WindowPresenter = Presenter
