#
""""""

from logging import getLogger
from pathlib import Path
from traceback import format_exc
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from albuswall.core import Application
from albuswall.configue.dynamic_declaration import dynamic_state
from albuswall.configue import (
    Configue, StatefulNamespace, StateField,
    setup_dynamic_config
)

from ..widgets import blur_overlay_label
from ..config import WindowPresenterConfs
from .window_resizer import WindowResizer

if TYPE_CHECKING:
    from albuswall.core import Container
    from ..window.window import Window

_logger = getLogger(__name__)

# TODO 仅在这里使用这个试验版的state功能，目前设计上还有问题
# WINDOW_STATE_FILENAME = "window.json"
WINDOW_STATE_FILENAME = Application.instance().configure.static.files.window_state


@dynamic_state(
    "window",
    ("path", "cache"),
    WINDOW_STATE_FILENAME,
    autosave=True
)
class WindowState(StatefulNamespace):
    x = StateField(int, default=None)
    y = StateField(int, default=None)
    width = StateField(int, default=800)
    height = StateField(int, default=600)
    maximized = StateField(bool, default=False)

setup_dynamic_config(
    Application.instance().container,
    modules=["albuswall.ui.builtin.presenter.window_presenter"],
    preload=["window"],   # 如果 teardown 之前一定要有 st，也可以靠这句提前加载
)


class WindowPresenter(QObject):
    """主窗口的视图调度 + 样式装载。

    只负责 view 层"谁听谁"和窗口级外观，不承担业务。
    只依赖 container 的 `_config`，基础设施变动对它无影响。
    """

    def __init__(self, target: "Window", parent=None) -> None:
        super().__init__(parent)
        self._target = target
        self.confs = WindowPresenterConfs()
        self.window_resizer = WindowResizer(target, target)

    # ---------------- lifecycle ----------------
    def setup(self, container: "Container") -> None:
        # config 是 bootstrap 必备项；缺失就让它抛，不要吞
        config: "Configue" = container.require("config")

        self.confs.ensure_theme_files_completed(config)
        self._target.resize(800, 600)
        self._apply_style_sheet(config)

        # 等一次事件循环，布局稳定后再同步 title_bar 高度
        QTimer.singleShot(0, self._sync_title_bar_height)

        self._wire_navigation()

    def teardown(self) -> None:
        self._target.removeEventFilter(self)

        st = Application.instance().configure.dynamic.window
        g = self._target.normalGeometry()
        st.x, st.y = g.x(), g.y()
        st.width, st.height = g.width(), g.height()
        st.maximized = self._target.isMaximized()
        st.save()

    # ---------------- internals ----------------
    def _sync_title_bar_height(self) -> None:
        try:
            self.window_resizer.title_bar_height = (
                self._target.title_bar.action_bar_bottom_y_in_parent
            )
        except AttributeError as exc:
            _logger.warning("title_bar height sync failed: %s", exc)

    def _wire_navigation(self) -> None:
        w = self._target
        overlay = w.overlay

        # (signal, slot) 表驱动
        pairs = (
            (w.title_bar.album_button.clicked,
             lambda: overlay.setCurrentWidget(w.album)),
            (w.album.close_button.clicked, overlay.show_blank),

            (w.menu.source_action.triggered,
             lambda: overlay.setCurrentWidget(w.source)),
            (w.source.close_button.clicked, overlay.show_blank),

            (w.menu.setting_action.triggered,
             lambda: overlay.setCurrentWidget(w.setting)),
            (w.setting.close_button.clicked, overlay.show_blank),

            (w.detail.close_button.clicked, w.detail.close_requested.emit),
            (w.detail.close_requested, overlay.show_blank),
        )
        for signal, slot in pairs:
            try:
                signal.connect(slot)
            except Exception as e:
                _logger.error(
                    "UI wire err: %s\ntrackbar: %s",
                    e,
                    format_exc()
                )

    def _apply_style_sheet(self, _: "Configue") -> None:
        qss_path = self.confs.qss
        if not qss_path:
            _logger.info(
                "No QSS path resolved (ui.style_sheet unset and "
                "path.theme missing); skip stylesheet."
            )
            return

        qss = Path(qss_path)
        if not qss.is_file():
            _logger.warning("QSS file is not available: %s", qss)
            return

        try:
            self._target.setStyleSheet(qss.read_text(encoding="utf-8"))
        except OSError as exc:
            _logger.warning("Failed to read QSS %s: %s", qss, exc)
            return

        # 加载 QSS 后关闭 blur label 文本绘制（既定视觉方案）
        blur_overlay_label.DRAW_LABEL_CONTENT = False
        _logger.debug("Loaded QSS from %s", qss)

    # ---------------- view ----------------
    def show_detail(self) -> None:
        """把详情视图推到栈顶。

        展示哪张资产由 ViewerPresenter.open(ctx) 决定；这里不查路径、
        不加载 QPixmap、不调 detail.set_image。
        """
        self._target.overlay.setCurrentWidget(self._target.detail)
        # self._target.detail.raise_()

    def hide_detail(self) -> None:
        """收起详情视图。"""
        self._target.overlay.show_blank()
