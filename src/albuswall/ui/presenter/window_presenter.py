#
"""窗口 presenter：窗口级导航 + 焦点调度 + 提示条 + 主题图标。

职责边界
========

``Window`` 自己已经接好了「纯视图」那部分内部跳转（标题栏相册按钮、菜单
打开导入源、各页关闭按钮），本 presenter 只补视图不该知道的几件事：

1. **跨页的焦点调度**：浮层切换后把键盘焦点带进新页面（空白页回到内容
   网格），纯键盘用户不会「焦点掉在已隐藏的控件上」；
2. **窗口级反馈**：其它 presenter 的错误 / 提示统一走 ``window.show_toast``；
   菜单里「设置」这类还没有实现的动作，也给一条明确提示而不是静默吞掉；
3. **主题图标**：把主题目录 ``<theme>/icons/`` 里的图标装到标题栏工具按钮
   与详情页动作按钮上（图标不参与翻译，文案落到 tooltip），并同步窗口 /
   应用图标。样式表只负责「48px 玻璃圆 + icon-size」，图标由这里装。

窗口几何持久化、QSS 本体**不**在这里：样式表归
:class:`~albuswall.ui.presenter.main.MainPresenter`，几何持久化等
``configue`` 提供运行期状态段后再补。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .base import Presenter, register_presenter
from .theme import Theme

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["WindowPresenter"]

logger = logging.getLogger(__package__)

#: 标题栏工具按钮 → 主题图标名（``<theme>/icons/<name>.svg``）。
TITLE_BAR_ICONS: tuple[tuple[str, str], ...] = (
    ("album", "album_button"),
    ("search", "search_button"),
    ("extra", "more_button"),
)

#: 详情页「只有图标」的动作按钮 —— 目前只有收藏有主题图标。
DETAIL_ICONS: tuple[tuple[str, str], ...] = (
    ("favourite_button", "favorite_icon"),
)


class WindowPresenter(Presenter):
    """主窗口的视图调度者。"""

    name = "window"

    #: 用于无法直接 toast 的场景（例如子组件只发信号时）。
    toast_requested = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._ready_hooked = False

    # ------------------------------------------------------------------ #
    # 视图
    # ------------------------------------------------------------------ #
    @property
    def window(self) -> Window:
        return self._window

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        self._wire()
        # 主 presenter 启动时已经套好主题：这里把主题图标补上
        self.apply_theme(self._current_theme())
        # 等一次事件循环：布局稳定后焦点才有正确的父链。
        QTimer.singleShot(0, self._window.focus_default_region)

    def _wire(self) -> None:
        w = self._window
        try:
            w.overlay.currentChanged.connect(self._on_overlay_changed)
        except (AttributeError, RuntimeError, TypeError):
            logger.warning("window presenter: overlay not available")

        # 「设置」页尚未重建：给一条明确提示，不留死按钮。
        try:
            w.menu.setting_action.triggered.connect(
                self._on_settings_requested)
        except (AttributeError, RuntimeError, TypeError):
            logger.warning("window presenter: setting action not available")

        self.toast_requested.connect(self.toast)

    # ------------------------------------------------------------------ #
    # 主题图标
    # ------------------------------------------------------------------ #
    def _current_theme(self) -> Theme | None:
        """从主 presenter 取当前主题（拿不到就返回 ``None``）。"""
        parent = self.parent
        theme = getattr(parent, "theme", None)
        return theme if isinstance(theme, Theme) else None

    def apply_theme(self, theme: Theme | None) -> None:
        """把主题图标装到窗口上（``theme`` 为 ``None`` = 全部回退文字）。"""
        self._apply_title_bar_icons(theme)
        self._apply_detail_icons(theme)
        self._apply_window_icon(theme)

    def _apply_title_bar_icons(self, theme: Theme | None) -> None:
        title_bar = self._window.title_bar
        for button_name, icon_name in TITLE_BAR_ICONS:
            path = theme.icon(icon_name) if theme is not None else None
            if path is not None:
                title_bar.set_button(button_name, icon=path)
                continue
            # 主题里没有这张图：回到文字模式（默认文案由视图自己维护）
            title_bar.set_button(button_name)

    def _apply_detail_icons(self, theme: Theme | None) -> None:
        detail = self._window.detail
        for attr_name, icon_name in DETAIL_ICONS:
            button = getattr(detail, attr_name, None)
            if button is None:
                continue
            path = theme.icon(icon_name) if theme is not None else None
            detail.set_action_icon(button, path)

    def _apply_window_icon(self, theme: Theme | None) -> None:
        """窗口 / 应用图标：优先主题里的 ``app_icon``，否则用随包资源。"""
        path = theme.icon("app_icon") if theme is not None else None
        if path is None:
            try:
                from albuswall import resources

                path = resources.app_icon
            except (FileNotFoundError, KeyError, ValueError):
                path = None
        if path is None:
            return
        icon = QIcon(str(path))
        if icon.isNull():
            return
        self._window.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)

    # ------------------------------------------------------------------ #
    # 对外接口（其它 presenter / 主 presenter 用）
    # ------------------------------------------------------------------ #
    def toast(self, message: str, *, timeout_ms: int = 2500) -> None:
        """在窗口底部弹一条轻提示（空消息忽略）。"""
        try:
            self._window.show_toast(str(message), timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - 提示失败不该影响业务
            logger.exception("window presenter: toast failed")

    def show_detail(self) -> None:
        """把详情页推到浮层最前。"""
        self._window.overlay.setCurrentWidget(self._window.detail)

    def hide_detail(self) -> None:
        """收起当前浮层页（回到空白层）。"""
        self._window.overlay.show_blank()

    def focus_default_region(self) -> None:
        """窗口没有焦点时把焦点交给内容网格。"""
        if self._window.focusWidget() is None:
            self._window.content.setFocus()

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_overlay_changed(self, _index: int) -> None:
        """浮层切换 → 焦点跟随到新页面的主区域；空白页回内容网格。"""
        w = self._window
        overlay = w.overlay
        if overlay.is_blank():
            w.content.setFocus()
            return

        current = overlay.currentWidget()
        if current is w.album:
            target = w.album.album_view
        elif current is w.source:
            target = w.source.view
        elif current is w.detail:
            target = w.detail.image_viewer
        else:
            target = None
        if target is not None:
            target.setFocus()

    def _on_settings_requested(self) -> None:
        self.toast(self.tr("设置页尚未实现"))


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_window_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("window presenter skipped: no window bound")
        return None
    return WindowPresenter(container, window=window, parent=main)
