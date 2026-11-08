#
"""albusWall 主窗口 —— 纯视图层。

:class:`Window` 只做四件事：

1. **搭图层**：背景模糊层 → 内容层 → overlay 浮层 → 标题栏；
2. **同步几何**：窗口 resize 时把各层铺满 / 对齐到标题栏下沿；
3. **组装子控件**：标题栏、内容区、相册页、导入源页、详情页、设置页、菜单；
4. **补回窗口交互**：自绘边框（``FramelessWindowHint``）没有系统标题栏，
   拖拽移动 / 边缘缩放由 :class:`~albuswall.ui.platform.window_resizer.
   WindowResizer` 装在窗口上代劳。

它**不**查询数据、不调度服务、不安装快捷键、不读配置、不认识任何
model / delegate / DTO。所有业务动作都由外部（插件 / presenter）通过
子控件暴露的信号接线；要换内容，直接往 ``overlay`` 里换页面控件即可。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from ..platform import WindowResizer
from ..utils import auto_set_object_names
from ..widgets import BlurLabel, PassthroughStack
from .album import Album
from .content import Content
from .detail import Detail
from .menu import Menu
from .settings_page import SettingsPage
from .source import IngestSource
from .titlebar import TitleBar
from .utils import FocusIsolator

__all__ = ["Window"]

#: 带这些修饰键的 Esc 不参与窗口的 Esc 层级（留给其它消费者）。
# noinspection unsupported-operator
_ESCAPE_IGNORED_MODIFIERS = (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.MetaModifier
)


class Window(QWidget):
    """albusWall 主窗口。

    layer（自下而上）::

        Window
        ├─ back_ground            背景模糊层，铺满整窗口
        ├─ back_ground_screen_area 模糊层的取景目标，铺满整窗口
        ├─ content                内容层（相册网格），铺满整窗口，压在最底
        ├─ title_bar              标题栏，让覆盖层漏个标题栏出来，工具栏盖住
        └─ overlay                浮层，从标题栏下沿铺到窗口底部
            ├─ album              相册页
            ├─ source             导入源页
            ├─ detail             详情页
            └─ settings_page      设置页
    """

    main_layout: QVBoxLayout

    back_ground: BlurLabel
    back_ground_screen_area: QWidget

    title_bar: TitleBar
    content: Content
    overlay: PassthroughStack
    album: Album
    source: IngestSource
    detail: Detail
    settings_page: SettingsPage
    menu: Menu
    window_resizer: WindowResizer

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # 轻量提示条（toast）：懒创建，只在本窗口内显示
        self._toast_timer: QTimer | None = None
        self.toast_label: QLabel | None = None

        # overlay 打开期间的焦点隔离器：只改 focusPolicy，不动 enabled /
        # 可见性 / 样式（业务在隔离期间对 enabled 的修改原样保留）。
        self._focus_isolator = FocusIsolator()
        self._overlay_active: bool = False

        self._setup_window()
        self._setup_ui()
        # 唯一同步点：PassthroughStack 继承 QStackedWidget，每次换页（含回
        # 空白页）都发 currentChanged，所以 show_album / close_overlay 等
        # 公开入口不重复调用，避免双触发。
        self.overlay.currentChanged.connect(self._sync_focus_scope)
        auto_set_object_names(self)
        # 标题栏镜像窗口标题：构造期可能已有人设过 title，这里补一次同步。
        self._sync_window_title()

    # ------------------------------------------------------------------ #
    # 窗口标题
    # ------------------------------------------------------------------ #
    def setWindowTitle(self, title: str = "") -> None:  # noqa: N802 - Qt 命名
        """重写 Qt 接口：设置窗口标题，并同步到标题栏。

        这是唯一的对外入口——``self.setWindowTitle("...")`` 之后，标题栏
        操作栏左侧的标题自动跟着变。自绘边框（``FramelessWindowHint``）没有
        系统标题栏，这个标签就是「窗口标题」的唯一可见呈现。
        """
        super().setWindowTitle(title)
        self._sync_window_title()

    def set_window_title(self, title: str) -> None:
        """:meth:`setWindowTitle` 的 snake_case 别名。"""
        self.setWindowTitle(title)

    def _sync_window_title(self) -> None:
        """把窗口标题写进标题栏（``title_bar`` 还没建时静默跳过）。"""
        title_bar = getattr(self, "title_bar", None)
        if title_bar is not None:
            # noinspection unresolved-references
            title_bar.set_window_title(self.windowTitle())

    def changeEvent(self, event):  # noqa: N802 - Qt 命名
        """兜底：任何途径改了窗口标题都同步到标题栏。

        除了 :meth:`setWindowTitle`，Qt 内部（如窗口被重新设置属性）也可能
        发 ``WindowTitleChange``；这里再兜一层，避免标题栏和窗口标题失联。
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowTitleChange:
            self._sync_window_title()

    # ------------------------------------------------------------------ #
    # 窗口属性
    # ------------------------------------------------------------------ #
    def _setup_window(self) -> None:
        self.setObjectName(type(self).__name__)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)

        # 自绘边框把系统标题栏和原生边框一起去掉了，拖拽移动 / 边缘缩放没有
        # 任何东西负责，必须自己补（见 albuswall.ui.platform.window_resizer）：
        # 它挂在窗口上、在应用层过滤鼠标事件，只在「标题栏区域」或「边缘
        # ``margin`` 像素」上生效，按钮 / 输入框等交互控件照常收到点击。
        self.window_resizer = WindowResizer(self, self)

    # ------------------------------------------------------------------ #
    # 图层
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.main_layout = layout
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 背景模糊层 ----
        self.back_ground = BlurLabel(self)
        self.back_ground_screen_area = QWidget(self)
        self.back_ground.target_widget = self.back_ground_screen_area
        # 取景目标只被 ``BlurLabel.grab()`` 读取，从不处理交互。它铺满整窗、
        # 且在层叠顺序里位于内容层之上（见 _setup_ui 末尾的 raise_/lower_），
        # 所以必须对所有鼠标事件透明 —— 否则它会吞掉内容层的点击与滚轮，
        # 表现为「内容页既不能滚动、也点不开详情」。
        for widget in (self.back_ground, self.back_ground_screen_area):
            widget.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.setContextMenuPolicy(
                Qt.ContextMenuPolicy.NoContextMenu)

        # ---- 标题栏 ----
        self.title_bar = TitleBar(self)

        # ---- 内容层 ----
        self.content = Content(self)
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.title_bar.set_passthrough_target(self.content)

        # ---- 浮层：相册 / 导入源 / 详情 ----
        self.overlay = PassthroughStack(self)

        self.album = Album(target=self, parent=self.overlay)
        self.overlay.addWidget(self.album)

        self.source = IngestSource(parent=self, target=self)
        self.overlay.addWidget(self.source)

        self.detail = Detail(self)
        self.detail.target_widget = self
        self.overlay.addWidget(self.detail)

        self.settings_page = SettingsPage(target=self, parent=self.overlay)
        self.overlay.addWidget(self.settings_page)

        # ---- 标题栏右侧「更多」菜单 ----
        self.menu = Menu(self)
        self.title_bar.extra_button.setMenu(self.menu)

        # ---- 视图内接线：切页 / 收起 / 退出（都是窗口自身行为，不涉及业务）----
        self.title_bar.album_button.clicked.connect(self.toggle_album)
        self.menu.source_action.triggered.connect(self.toggle_source)
        self.menu.exit_action.triggered.connect(self.request_exit)

        self.album.close_button.clicked.connect(self.close_overlay)
        self.source.close_requested.connect(self.close_overlay)
        self.detail.close_requested.connect(self.close_overlay)
        self.settings_page.close_requested.connect(self.close_overlay)

        # ---- 层级与装配 ----
        self.overlay.raise_()
        self.content.lower()
        self.back_ground.lower()

        layout.addWidget(self.title_bar)
        layout.addStretch()

    # ------------------------------------------------------------------ #
    # 图层切换（只换浮层当前页，不查数据、不知道页里有什么）
    # ------------------------------------------------------------------ #
    def toggle_album(self) -> None:
        """标题栏「相册」按钮：打开 / 收起相册页。"""
        self._toggle_page(self.album)

    def toggle_source(self) -> None:
        """「更多 → 导入源」菜单：打开 / 收起导入源页。"""
        self._toggle_page(self.source)

    def show_album(self) -> None:
        """显式打开相册页（不切换）。"""
        self.overlay.setCurrentWidget(self.album)

    def show_source(self) -> None:
        """显式打开导入源页（不切换）。"""
        self.overlay.setCurrentWidget(self.source)

    def show_settings(self) -> None:
        """显式打开设置页（不切换）。"""
        self.overlay.setCurrentWidget(self.settings_page)

    def toggle_settings(self) -> None:
        """打开 / 收起设置页。"""
        self._toggle_page(self.settings_page)

    def close_overlay(self) -> None:
        """收起当前浮层页，回到空白层（内容层重新可交互）。"""
        self.overlay.show_blank()

    def close_layer(self) -> bool:
        """``window.close``：收起当前浮层页；没有浮层页时关窗口。

        Returns:
            ``True`` = 只收起了浮层页（窗口还在）；``False`` = 关的是窗口。
        """
        if self.overlay.is_blank():
            self.close()
            return False
        self.close_overlay()
        return True

    def toggle_fullscreen(self) -> bool:
        """``window.fullscreen``：全屏 / 还原；返回切换**之后**是否全屏。"""
        if self.isFullScreen():
            self.showNormal()
            return False
        self.showFullScreen()
        return True

    def focus_default_region(self) -> None:
        """把键盘焦点交给内容网格（外部在窗口显示 / 收起浮层时调用）。"""
        self.content.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------ #
    # 键盘：Esc 的层级
    # ------------------------------------------------------------------ #
    #
    # Esc 分两步，Shift+Esc 一步到位：
    #
    # * **Shift+Esc** —— 直接收起叠加层（回到空白占位页），不看焦点；
    # * **Esc（第一次）** —— 取消焦点：把焦点从当前控件上摘掉，叠加层保持
    #   原样；
    # * **Esc（焦点已取消时）** —— 收起叠加层。
    #
    # 「取消焦点」不换成「把焦点挪到空白页」：空白页是 NoFocus，挪过去等于
    # 焦点还挂在某个控件上，「取消」不可观测；这里直接 ``clearFocus()``，
    # 之后按 Esc 由 Qt 把按键交给窗口本身（没有焦点控件时的回退目标）。
    #
    # 按键先到焦点控件：搜索栏这类自己消费 Esc 的控件（先收建议列表、再收
    # 搜索栏）优先；没被消费的 Esc 沿控件链冒泡到窗口，这一段才生效。
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event):  # noqa: N802 - Qt 命名
        """Esc：先取消焦点、再收起叠加层；Shift+Esc：直接收起叠加层。"""
        mods = event.modifiers()
        if (event.key() == Qt.Key.Key_Escape
                and not mods & _ESCAPE_IGNORED_MODIFIERS):
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.close_overlay()
            elif not self.cancel_focus():
                # 焦点已经取消（或本来就没有）：第二步 = 收起叠加层
                self.close_overlay()
            event.accept()
            return
        super().keyPressEvent(event)

    def cancel_focus(self) -> bool:
        """取消本窗口内的键盘焦点；本来就没有焦点控件时返回 ``False``。"""
        focused = self.focusWidget()
        # noinspection unreachable-code
        if focused is None:
            return False
        focused.clearFocus()
        return True

    # ------------------------------------------------------------------ #
    # overlay 焦点隔离
    # ------------------------------------------------------------------ #
    #
    # Qt 的 Tab 链只看焦点策略与可见性，不看视觉遮挡。overlay 从 action_bar
    # 下沿铺开、盖住 tool_bar，content 垫在底下 —— 这三者在物理上是兄弟层，
    # 所以 overlay 打开后 Tab 仍能走到被盖住的网格与工具按钮上，空格还会
    # 误触发它们。这里按 overlay 状态把它们「逻辑上」移出 Tab 链。
    #
    # 不用 setEnabled(False)：那会覆盖业务在隔离期间对 enabled 的动态修改
    # （例如「无选中时编辑按钮禁用」），并触发 :disabled 样式。FocusIsolator
    # 只改 focusPolicy，鼠标点击、绘制、enabled 都保持原样。
    #
    # 非当前页不在这个清单里：PassthroughStack 用 QStackedWidget 的换页
    # 语义，非 current 页会被 hide() 掉（实测 isVisible() 为 False），本来
    # 就不在 Tab 链里，无需额外隔离。
    # ------------------------------------------------------------------ #
    def _sync_focus_scope(self) -> None:
        """按 overlay 当前状态，决定隔离哪些子树（唯一入口）。"""
        self._focus_isolator.restore()

        if self.overlay.is_blank():
            self._overlay_active = False
            return

        self._overlay_active = True
        self._focus_isolator.isolate(self.content)
        self._focus_isolator.isolate(self.title_bar.tool_bar)

        # 沉浸式详情：窗口动作条（min/max/close）一起隔离，Tab 不离开详情页
        # 也不落到窗口按钮上。若希望这些按钮在详情页常驻可 Tab，删掉下面
        # 这一行即可（它们目前本身都是 NoFocus，这里把意图写成代码）。
        if self.overlay.currentWidget() is self.detail:
            self._focus_isolator.isolate(self.title_bar.action_bar)

        self._ensure_focus_outside_isolated()

    def _ensure_focus_outside_isolated(self) -> None:
        """焦点若落在刚被隔离的子树里，挪回当前 overlay 页。"""
        app = QApplication.instance()
        if app is None:
            return
        app: QApplication
        focused = app.focusWidget()
        if focused is None or not self._focus_isolator.is_isolated(focused):
            return
        page = self.overlay.currentWidget()
        if page is None:
            return
        target = self._first_focusable(page)
        if target is None:
            target = page
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _first_focusable(root: QWidget) -> QWidget | None:
        """``root`` 子树里第一个可 Tab 到的控件（找不到返回 ``None``）。"""
        for widget in (root, *root.findChildren(QWidget)):
            if not widget.isVisible() or not widget.isEnabled():
                continue
            if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                continue
            return widget
        return None

    def _toggle_page(self, page: QWidget) -> None:
        if self.overlay.currentWidget() is page:
            self.overlay.show_blank()
        else:
            self.overlay.setCurrentWidget(page)

    def request_exit(self) -> None:
        """「更多 → 退出」（``app.quit`` 快捷键也走这里）：

        先请求停事件循环，再关窗口。

        注意 Qt 行为：``quit()`` 会先请求关闭窗口；任一窗口在
        ``closeEvent`` 里 ``ignore()`` 时，这次 quit 会被中止，事件循环
        继续跑。因此本函数的"退出"语义**可被外部取消**——需要无条件退出
        应改 ``app.exit(0)``（不经过 closeEvent，但会跳过关闭清理）。

        当前选择可取消语义：外部（如未保存确认对话框）有权拦下退出。
        """
        app = QApplication.instance()
        if app is not None:
            app.quit()
        self.close()

    # ------------------------------------------------------------------ #
    # 几何同步
    # ------------------------------------------------------------------ #
    def _update_overlay_geometry(self) -> None:
        y = self.title_bar.action_bar_bottom_y_in_parent + 1
        self.overlay.setGeometry(
            0, y, self.width(), max(0, self.height() - y))

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        self.back_ground.setGeometry(self.rect())
        self.back_ground_screen_area.setGeometry(self.rect())
        self.content.setGeometry(self.rect())
        self._update_overlay_geometry()
        self._place_toast()
        super().resizeEvent(event)

    # ------------------------------------------------------------------ #
    # 轻量提示（toast）
    # ------------------------------------------------------------------ #
    def show_toast(self, message: str, *, timeout_ms: int = 2500) -> None:
        """在窗口底部短暂显示一条消息；空消息直接忽略。

        只在主窗口内部绘制，不弹对话框；连续调用会重置计时器。
        """
        text = str(message or "").strip()
        if not text:
            return
        label = self._ensure_toast()
        label.setText(text)
        label.adjustSize()
        self._place_toast()
        label.show()
        label.raise_()
        self._ensure_toast_timer().start(max(200, int(timeout_ms)))

    def _ensure_toast(self) -> QLabel:
        label = self.toast_label
        if label is None:
            label = QLabel("", self)
            label.setObjectName("WindowToast")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.hide()
            self.toast_label = label
        return label

    def _ensure_toast_timer(self) -> QTimer:
        timer = self._toast_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._hide_toast)
            self._toast_timer = timer
        return timer

    def _hide_toast(self) -> None:
        if self.toast_label is not None:
            self.toast_label.hide()

    def _place_toast(self) -> None:
        label = self.toast_label
        if label is None:
            return
        size = label.sizeHint()
        label.resize(size)
        x = max(0, (self.width() - size.width()) // 2)
        y = max(0, self.height() - size.height() - 32)
        label.move(x, y)


if __name__ == "__main__":
    # noinspection unresolved-references, broad-exception
    def _main() -> int:  # pragma: no cover - 手动预览入口
        import sys

        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)

        # 有主题就套上；没有也能正常显示（不依赖业务层）
        try:
            from albuswall.resources import qss

            app.setStyleSheet(qss.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 预览入口，资源缺失不影响显示
            pass

        window = Window()
        window.setWindowTitle("albusWall")
        window.resize(960, 720)
        window.show()
        return app.exec()


    raise SystemExit(_main())
