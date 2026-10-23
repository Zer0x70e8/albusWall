#
"""主窗口 presenter：样式装载 + 窗口几何持久化 + 顶层导航接线。

不承担任何业务：业务动作在各自的 presenter 里，这里只负责
「谁听谁」和窗口级外观。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QTimer

from albuswall.configue import Configue, StateField, StatefulNamespace
from albuswall.configue.dynamic_declaration import dynamic_state
from albuswall.core import Application
from albuswall.log import Logger, getLogger

from ..config import WindowPresenterConfs
from ..platform import is_tiling
from ..platform.window_resizer import WindowResizer
from ..widgets import blur_overlay_label

if TYPE_CHECKING:
    from albuswall.core import Container

    from ..window.window import Window

__all__ = ["WindowPresenter", "WindowState"]

_logger: Logger = getLogger(__name__)  # type: ignore

WINDOW_STATE_FILENAME = Application.instance().configure.static.files.window_state


@dynamic_state(
    "window",
    ("path", "cache"),
    WINDOW_STATE_FILENAME,
    # 关闭逐字段自动落盘：位置变更由 WindowPresenter 做 debounce 后一次性
    # save()（见 _persist_window_state），避免拖动窗口时每个字段写一次盘。
    autosave=False,
)
class WindowState(StatefulNamespace):
    """窗口几何在 ``dynamic.window`` 下的字段声明。"""

    x = StateField(int, default=None)
    y = StateField(int, default=None)
    width = StateField(int, default=800)
    height = StateField(int, default=600)
    maximized = StateField(bool, default=False)


# 说明：这里不再自调用 setup_dynamic_config。
# 模块被 import 时 @dynamic_state 已登记进 DYNAMIC_REGISTRY，
# `config.dynamic.window` 会经 DynamicConfig.__getattr__ 懒加载。


class WindowPresenter(QObject):
    """主窗口的视图调度 + 样式装载。

    只负责 view 层"谁听谁"和窗口级外观，不承担业务。
    只依赖 container 的 ``config``，基础设施变动对它无影响。
    """

    # 窗口几何变更后延迟落盘的毫秒数：拖动 / 缩放期间只重置计时器，
    # 停下来（或最大化状态切换后）才真正写一次 window.json。
    _SAVE_DEBOUNCE_MS = 400

    #: 视为「几何变了」的事件类型。
    _GEOMETRY_EVENTS = frozenset({
        QEvent.Type.Move,
        QEvent.Type.Resize,
        QEvent.Type.WindowStateChange,
    })

    def __init__(self, target: "Window", parent=None) -> None:
        super().__init__(parent)
        self._target = target
        self.confs = WindowPresenterConfs()
        self.window_resizer = WindowResizer(target, target)

        # 平铺式窗口管理器自己管几何，我们不恢复也不落盘
        self._manage_geometry = not is_tiling()

        # 动态配置子树：首次访问时经 DYNAMIC_REGISTRY 懒加载
        self._state: WindowState | None = None
        self._restoring = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self._SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._persist_window_state)

    # ---------------- lifecycle ----------------
    def setup(self, container: "Container") -> None:
        # config 是 bootstrap 必备项；缺失就让它抛，不要吞
        config: "Configue" = container.require("config")

        self.confs.ensure_theme_files_completed(config)
        self._target.resize(800, 600)
        self._apply_style_sheet()

        # 等一次事件循环，布局稳定后再同步 title_bar 高度
        QTimer.singleShot(0, self._sync_title_bar_height)

        self._wire_navigation()

        if not self._manage_geometry:
            _logger.info(
                "The tiling desktop environment "
                "doesn't automatically manage window geometry."
            )
            return

        # 恢复之后再开始跟踪，避免把恢复动作当成用户改窗口
        self._restore_window_state()
        self._install_geometry_tracking()

    def teardown(self) -> None:
        if self._manage_geometry:
            self._target.removeEventFilter(self)
            self._save_timer.stop()
            # 退出前把最后一次几何变更（可能还在 debounce 窗口里）写掉
            self._persist_window_state()

    # ---------------- 窗口位置持久化 ----------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """几何变更（移动 / 缩放 / 最大化切换）→ 重置 debounce 计时器。"""
        if (
                self._manage_geometry
                and obj is self._target
                and not self._restoring
                and event.type() in self._GEOMETRY_EVENTS
        ):
            self._save_timer.start()
        return super().eventFilter(obj, event)

    def _install_geometry_tracking(self) -> None:
        self._target.installEventFilter(self)

    # noinspection broad-exception
    def _load_state(self) -> "WindowState | None":
        """取动态配置子树；失败只记日志，不让窗口启动 / 退出失败。"""
        if self._state is not None:
            return self._state
        try:
            from albuswall.configue.declaration import get_config
            self._state = get_config().dynamic.window
        except Exception:
            _logger.exception("window state unavailable, skip persistence")
            return None
        return self._state

    # noinspection broad-exception
    def _persist_window_state(self) -> None:
        """把当前几何写进 window.json（一次 save，抗异常退出）。"""
        if not self._manage_geometry:  # 双保险：QTimer 也可能被别处 start
            return
        state = self._load_state()
        if state is None:
            return
        g = self._target.normalGeometry()
        try:
            state.x, state.y = g.x(), g.y()
            state.width, state.height = g.width(), g.height()
            state.maximized = self._target.isMaximized()
            state.save()
        except Exception:
            _logger.exception("window state save failed")

    # ---------------- internals ----------------
    def _sync_title_bar_height(self) -> None:
        try:
            self.window_resizer.title_bar_height = (
                self._target.title_bar.action_bar_bottom_y_in_parent
            )
        except AttributeError as exc:
            _logger.warning("title_bar height sync failed: %s", exc)

    def _wire_navigation(self) -> None:
        """标题栏按钮 / 菜单项 → overlay 切换。表驱动，一条一条连。"""
        w = self._target
        overlay = w.overlay
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
            except (RuntimeError, TypeError) as exc:
                # 缺控件 / 签名不匹配属于装配问题，记下来但不拖垮整窗
                _logger.warning(
                    "UI wire failed: %s -> %s (%s)", signal, slot, exc,
                )

    def _apply_style_sheet(self) -> None:
        """把 QSS 装到窗口上（路径由 ``ui.style_sheet`` 解析好）。"""
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

    def hide_detail(self) -> None:
        """收起详情视图。"""
        self._target.overlay.show_blank()

    def _restore_window_state(self) -> None:
        """上一次的窗口几何（缺失 / 损坏时保持 setup 的默认 800x600）。"""
        st = self._load_state()
        if st is None:
            return

        self._restoring = True
        try:
            if st.width and st.height:
                self._target.resize(st.width, st.height)
            if st.x is not None and st.y is not None:
                self._target.move(st.x, st.y)
            if st.maximized:
                self._target.showMaximized()
        finally:
            self._restoring = False
            # 丢弃恢复过程中（resize/move）产生的待保存
            self._save_timer.stop()
