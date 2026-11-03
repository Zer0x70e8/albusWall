#
"""albusWall 主窗口 —— 纯视图层。

:class:`Window` 只做三件事：

1. **搭图层**：背景模糊层 → 内容层 → overlay 浮层 → 标题栏；
2. **同步几何**：窗口 resize 时把各层铺满 / 对齐到标题栏下沿；
3. **组装子控件**：标题栏、内容区、相册页、导入源页、详情页、菜单。

它**不**查询数据、不调度服务、不安装快捷键、不读配置、不认识任何
model / delegate / DTO。所有业务动作都由外部（插件 / presenter）通过
子控件暴露的信号接线；要换内容，直接往 ``overlay`` 里换页面控件即可。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget, QApplication

from ..utils import auto_set_object_names
from ..widgets.blur_overlay_label import BlurLabel
from ..widgets.passthrough_stack_widget import PassthroughStack

from .album import Album
from .content import Content
from .detail import Detail
from .menu import Menu
from .source import IngestSource
from .titlebar import TitleBar

__all__ = ["Window"]


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
            └─ detail             详情页
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
    menu: Menu

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # 轻量提示条（toast）：懒创建，只在本窗口内显示
        self._toast_timer: QTimer | None = None
        self.toast_label: QLabel | None = None

        self._setup_window()
        self._setup_ui()
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

        # ---- 标题栏右侧「更多」菜单 ----
        self.menu = Menu(self)
        self.title_bar.extra_button.setMenu(self.menu)

        # ---- 视图内接线：切页 / 收起 / 退出（都是窗口自身行为，不涉及业务）----
        self.title_bar.album_button.clicked.connect(self.toggle_album)
        self.menu.source_action.triggered.connect(self.toggle_source)
        self.menu.exit_action.triggered.connect(self._on_exit_requested)

        self.album.close_button.clicked.connect(self.close_overlay)
        self.source.close_requested.connect(self.close_overlay)
        self.detail.close_requested.connect(self.close_overlay)

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

    def close_overlay(self) -> None:
        """收起当前浮层页，回到空白层（内容层重新可交互）。"""
        self.overlay.show_blank()

    def focus_default_region(self) -> None:
        """把键盘焦点交给内容网格（外部在窗口显示 / 收起浮层时调用）。"""
        self.content.setFocus(Qt.FocusReason.OtherFocusReason)

    def _toggle_page(self, page: QWidget) -> None:
        if self.overlay.currentWidget() is page:
            self.overlay.show_blank()
        else:
            self.overlay.setCurrentWidget(page)

    def _on_exit_requested(self) -> None:
        """「更多 → 退出」：先请求停事件循环，再关窗口。

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
