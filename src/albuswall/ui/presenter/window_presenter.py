#
"""窗口 presenter：窗口级导航 + 焦点调度 + 提示条 + 主题图标。

职责边界
========

``Window`` 自己已经接好了「纯视图」那部分内部跳转（标题栏相册按钮、菜单
打开导入源、各页关闭按钮），本 presenter 只补视图不该知道的几件事：

1. **跨页的焦点调度**：浮层切换后把键盘焦点带进新页面（空白页回到内容
   网格），纯键盘用户不会「焦点掉在已隐藏的控件上」；
2. **窗口级反馈**：其它 presenter 的错误 / 提示统一走 ``window.show_toast``；
   菜单 / 快捷键里的「设置」只发意图信号，真正的数据与写回在
   :class:`~albuswall.ui.presenter.settings_presenter.SettingsPresenter`；
3. **主题图标**：把主题里的图标装到标题栏工具按钮与详情页动作按钮上（图标不
   参与翻译，文案落到 tooltip），并同步窗口 / 应用图标。样式表只负责「48px
   玻璃圆 + icon-size」，图标由这里装。
   详情页的动作按钮目前**全部用文字**（``DETAIL_ICONS`` 为空），主题里的
   ``favorite_icon`` 归内容网格的收藏角标用。

图标**全部可配**：按钮 → 图标逻辑名（:data:`TITLE_BAR_ICONS`）→ ``[ui]`` 的
同名键，而那个键又是 ``[files]`` 里文件名与 ``${path:icons}`` / ``${ui:theme}``
的组合式（见 :mod:`albuswall.ui.presenter.theme`）。所以换图标有两种做法：
把文件放进 ``${path:theme}/<主题名>/icons/`` 覆盖，或改 ``[ui]`` 的键指向任意
文件；两者都不需要改代码。

窗口几何持久化也在这里，但**配置语义不在这一层**：读 / 写一律经
:mod:`albuswall.ui.window.settings`（界面层的配置使用接口，``[window]`` 段），
本类只负责把 ``QWidget`` 的几何 / 窗口状态翻译成那几条配置值，反过来把配置值
写回控件。所以本模块 import 了 Qt，而配置接口不 import Qt。

持久化时机：启动时 :meth:`WindowPresenter.restore_state` 恢复（早于窗口
``show()``，不会闪一下），退出时由 :class:`~albuswall.ui.presenter.main.
MainPresenter` 汇总所有 presenter 的 :meth:`Presenter.ui_state` 一次性提交
（见 ``MainPresenter.persist_ui_state``）。QSS 本体仍归主 presenter。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from albuswall.ui.window.settings import (
    StateSnapshot,
    geometry_text,
    store_window_state,
    window_state,
)

from .base import Presenter, register_presenter
from .theme import Theme

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["WindowPresenter"]

logger = logging.getLogger(__package__)

#: 标题栏工具按钮 → 图标逻辑名（``[ui]`` / ``[files]`` 的同名键，见
#: :mod:`albuswall.ui.presenter.theme`；不是文件名 —— 文件名在 ``[files]``）。
TITLE_BAR_ICONS: tuple[tuple[str, str], ...] = (
    ("album", "album_button"),
    ("search", "search_button"),
    ("extra", "more_button"),
)

#: 详情页「只有图标」的动作按钮 → 图标逻辑名（同上）。
#:
#: 目前**为空**：详情页五个动作按钮统一走文字。主题里的 ``favorite_icon``
#: （心形）是**缩略图角标**，由内容网格的委托画在已收藏的格子上
#: （见 :mod:`albuswall.ui.presenter.content_presenter`），不是按钮图标 ——
#: 48px 的圆按钮塞不下「Favorite」，和另外四个文字按钮也不一致。
DETAIL_ICONS: tuple[tuple[str, str], ...] = ()


class WindowPresenter(Presenter):
    """主窗口的视图调度者。"""

    name = "window"

    #: 用于无法直接 toast 的场景（例如子组件只发信号时）。
    toast_requested = Signal(str)
    #: 请求打开设置页（真正的数据/写回在 ``SettingsPresenter``）。
    settings_requested = Signal()

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
        # 恢复上次退出时的界面状态（几何 / 最大化 / 浮层页）。必须在窗口
        # ``show()`` 之前做：setup_ui 先启动 presenter 再 show，几何写在这里
        # 才不会先闪一个默认尺寸再跳。
        self.restore_state()
        # 等一次事件循环：布局稳定后焦点才有正确的父链。
        QTimer.singleShot(0, self._window.focus_default_region)

    def _wire(self) -> None:
        w = self._window
        try:
            w.overlay.currentChanged.connect(self._on_overlay_changed)
        except (AttributeError, RuntimeError, TypeError):
            logger.warning("window presenter: overlay not available")

        # 菜单「设置」：只发意图，SettingsPresenter 负责 dump / 写回。
        try:
            w.menu.setting_action.triggered.connect(self.open_settings)
        except (AttributeError, RuntimeError, TypeError):
            logger.warning("window presenter: setting action not available")

        self.toast_requested.connect(self.toast)

    # ------------------------------------------------------------------ #
    # 界面状态（``[window]`` 段；配置语义在 albuswall.ui.window.settings）
    # ------------------------------------------------------------------ #
    def restore_state(self) -> StateSnapshot:
        """按 ``[window]`` 段恢复几何 / 最大化 / 全屏 / 浮层页。

        只恢复「安全的值」：非法几何、未知浮层页在读接口里已经被过滤成空值，
        这里不需要再判断一次。返回读到的快照（调用方 / 测试可以据此断言）。

        用 ``setWindowState`` 而不是 ``showMaximized()``：后者会立刻把窗口
        显示出来，而此刻主 presenter 还没走完启动流程（样式表刚套上、子
        presenter 刚接线），提前 show 会闪一下。
        """
        snapshot = window_state()
        window = self._window

        rect = snapshot.rect()
        if rect is not None:
            window.setGeometry(QRect(*rect))

        state = window.windowState()
        if snapshot.maximized:
            state |= Qt.WindowState.WindowMaximized
        if snapshot.fullscreen:
            state |= Qt.WindowState.WindowFullScreen
        if state != window.windowState():
            window.setWindowState(state)

        if snapshot.overlay == "album":
            window.show_album()
        elif snapshot.overlay == "source":
            window.show_source()
        logger.debug(
            "window presenter: state restored (geometry=%s, maximized=%s, "
            "fullscreen=%s, overlay=%r)",
            snapshot.geometry or "-", snapshot.maximized,
            snapshot.fullscreen, snapshot.overlay or "")
        return snapshot

    def ui_state(self) -> dict[str, object]:
        """本次会话要记住的窗口状态（退出时由主 presenter 汇总提交）。"""
        window = self._window
        state = window.windowState()
        maximized = bool(state & Qt.WindowState.WindowMaximized)
        fullscreen = bool(state & Qt.WindowState.WindowFullScreen)
        # 最大化 / 全屏时 ``geometry()`` 是全屏尺寸，存下来会让下次还原
        # 尺寸失真；``normalGeometry()`` 是「还原后」的位置与大小。
        rect = window.geometry()
        if maximized or fullscreen:
            normal = window.normalGeometry()
            if normal.isValid():
                rect = normal
        return {
            "window.geometry": geometry_text(rect),
            "window.maximized": maximized,
            "window.fullscreen": fullscreen,
            "window.overlay": self._overlay_name(),
        }

    def save_state(self) -> dict[str, object]:
        """立即把窗口状态写回配置（设置页「立刻记住」按钮用）。

        退出路径不用它：那里由主 presenter 一次性提交全部 presenter 的状态，
        避免每关一个页面就落一次盘。
        """
        state = self.ui_state()
        try:
            return store_window_state(
                geometry=state["window.geometry"],
                maximized=state["window.maximized"],
                fullscreen=state["window.fullscreen"],
                overlay=state["window.overlay"],
            )
        except Exception:  # noqa: BLE001 - 记状态失败不影响界面
            logger.exception("window presenter: cannot persist window state")
            return {}

    def _overlay_name(self) -> str:
        """当前浮层页的名字（``""`` / ``album`` / ``source`` / ``detail``）。

        ``detail`` 不参与恢复：详情页需要「当前资产」，启动时不一定还有它。
        """
        window = self._window
        overlay = getattr(window, "overlay", None)
        if overlay is None:
            return ""
        try:
            if overlay.is_blank():
                return ""
            current = overlay.currentWidget()
        except (AttributeError, RuntimeError):
            return ""
        for name, page in (
                ("album", getattr(window, "album", None)),
                ("source", getattr(window, "source", None)),
                ("detail", getattr(window, "detail", None)),
        ):
            if page is not None and current is page:
                return name
        return ""

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
    # 窗口级动作（快捷键 app.* / window.* 的落点）
    # ------------------------------------------------------------------ #
    def open_settings(self) -> None:
        """``app.settings``：请求打开设置页。

        本 presenter 只发意图（``settings_requested``），数据 / 提交全部在
        :class:`~albuswall.ui.presenter.settings_presenter.SettingsPresenter`。
        同时也是标题栏菜单项「设置」的处理函数。
        """
        self.settings_requested.emit()

    def toggle_fullscreen(self) -> bool:
        """``window.fullscreen``：全屏 / 还原，返回切换后是否全屏。"""
        return self._window.toggle_fullscreen()

    def toggle_album(self) -> bool:
        """``window.album``：打开 / 收起相册页；返回操作后它是否在前台。"""
        self._window.toggle_album()
        return self._window.overlay.currentWidget() is self._window.album

    def toggle_source(self) -> bool:
        """``window.source``：打开 / 收起导入源页；返回操作后它是否在前台。"""
        self._window.toggle_source()
        return self._window.overlay.currentWidget() is self._window.source

    def close_layer(self) -> bool:
        """``window.close``：先收浮层页，没有浮层页时关窗口。"""
        return self._window.close_layer()

    def quit(self) -> None:
        """``app.quit``：请求停事件循环并关窗口（与菜单「退出」同一条路）。"""
        self._window.request_exit()

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
        elif current is getattr(w, "settings_page", None):
            # 设置页里焦点落到第一个可编辑控件；没有可编辑项时落关闭按钮。
            w.settings_page.focus_initial()
            return
        else:
            target = None
        if target is not None:
            target.setFocus()


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
