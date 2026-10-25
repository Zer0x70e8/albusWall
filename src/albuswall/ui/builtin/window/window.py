#
"""albusWall main window module.

Defines the top-level :class:`Window` of albusWall, which coordinates
the title bar, content area, overlay stack, and frosted-glass background.
"""

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
from PySide6.QtCore import Qt, QTimer

from ..widgets.blur_overlay_label import BlurLabel
from ..widgets.passthrough_stack_widget import PassthroughStack
from ..utils import auto_set_object_names

from .titlebar import TitleBar
from .content import Content
from .album import Album
from .menu import Menu
from .source import IngestSource
from .detail import Detail
from ..setting import Setting, StackStation as SettingStation


class Window(QWidget):
    """albusWall main window.

    layer（自下而上）::

        Window
        ├─ back_ground            背景，铺满整窗口
        ├─ stack_container        铺满整窗口
        │   └─ QStackedLayout(StackAll)
        │       ├─ content_page   内容层（占位符 + content）
        │       └─ overlay        浮层，叠在内容之上
        └─ title_bar              标题栏，raise 到最上
    """

    main_layout: QVBoxLayout

    back_ground: BlurLabel
    back_ground_screen_area: QWidget  # objname: #WindowTarget

    title_bar: TitleBar
    content: Content
    content_placeholder: QWidget
    overlay: PassthroughStack
    album: Album
    source: IngestSource
    menu: Menu
    setting: Setting
    detail: Detail
    setting_station: SettingStation

    def __init__(self, parent=None):
        super().__init__(parent)

        # 轻量提示条（动作失败 / TODO 反馈）：懒创建，只在本窗口内显示
        self._toast_label: QLabel | None = None
        self._toast_timer: QTimer | None = None

        self._setup()
        self._setup_ui()
        auto_set_object_names(self)
        self._attach_shortcuts()

    def _attach_shortcuts(self) -> None:
        """把注册中心的键装成本窗口上的 ``QShortcut``（幂等）。

        只有 QWidget 存在、``QApplication`` 已创建之后才可以把字符串键换成
        ``QKeySequence``；此时 ``factory.load()`` 已经应用过 ``[shortcuts]``，
        装上的就是最终生效的键位。失败只记日志，不能让快捷键挡住窗口创建。
        """
        try:
            from ..keymap import attach

            attach(self)
        except Exception:  # noqa: BLE001  键位子系统异常不阻塞窗口
            from logging import getLogger

            getLogger(__name__).exception(
                "keymap attach() failed, keyboard shortcuts disabled")

    def _setup(self):
        self.setObjectName(type(self).__name__)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self.main_layout = layout
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        #
        self.back_ground = BlurLabel(self)
        self.back_ground_screen_area = QWidget(self)

        self.back_ground.target_widget = self.back_ground_screen_area
        self.back_ground.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.back_ground.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back_ground.setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu
        )
        # TODO 相册没内容时候显示提示
        # self.back_ground.setText("<strong>Frosted Glass</strong>")

        #
        self.title_bar = TitleBar(self)
        self.title_bar.height_size_changed.connect(
            self._on_action_bar_size_changed
        )

        #
        self.content = Content(self)
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.title_bar.set_passthrough_target(self.content)

        #
        self.overlay = PassthroughStack(self)
        self.album = Album(target=self, parent=self.overlay)
        self.overlay.addWidget(self.album)

        #
        self.menu = Menu(self)
        self.menu.exit_action.triggered.connect(self.close)
        self.title_bar.extra_button.setMenu(self.menu)

        #
        self.source = IngestSource(parent=self, target=self)
        self.overlay.addWidget(self.source)

        #
        self.detail = Detail(self)
        self.detail.target_widget = self
        self.overlay.addWidget(self.detail)

        # TODO: 让设置页可浮动，但Qt还没适配好wayland
        # settingcontent_placeholder
        # self.setting = Setting(None)
        self.setting = Setting(parent=self, target=self)
        self.setting.enable_close_button = True
        # self.setting_station = SettingStation(self)
        # self.setting_station.set_widget(self.setting, self.tr("setting"))
        self.overlay.addWidget(self.setting)

        #
        self.overlay.raise_()
        self.content.lower()
        self.back_ground.lower()
        layout.addWidget(self.title_bar)
        layout.addStretch()

    # slot
    def _on_action_bar_size_changed(self, y: int) -> None:
        # +1 是原来的基线偏移，保留语义
        self.content_placeholder.setFixedHeight(y + 1)

    def _update_overlay_geometry(self):
        y = self.title_bar.action_bar_bottom_y_in_parent + 1
        # y = y + self.main_layout.spacing()
        self.overlay.setGeometry(
            0,
            y,
            self.width(),
            max(0, self.height() - y),
        )

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
        timer = self._ensure_toast_timer()
        timer.start(max(200, int(timeout_ms)))

    def _ensure_toast(self) -> QLabel:
        if self._toast_label is None:
            label = QLabel("", self)
            label.setObjectName("WindowToast")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.hide()
            self._toast_label = label
        return self._toast_label

    def _ensure_toast_timer(self) -> QTimer:
        if self._toast_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._hide_toast)
            self._toast_timer = timer
        return self._toast_timer

    def _hide_toast(self) -> None:
        if self._toast_label is not None:
            self._toast_label.hide()

    def _place_toast(self) -> None:
        label = self._toast_label
        if label is None:
            return
        size = label.sizeHint()
        label.resize(size)
        x = max(0, (self.width() - size.width()) // 2)
        y = max(0, self.height() - size.height() - 32)
        label.move(x, y)

    def resizeEvent(self, event):
        self.back_ground.setGeometry(self.rect())
        self.back_ground_screen_area.setGeometry(self.rect())
        self.content.setGeometry(self.rect())
        self._update_overlay_geometry()
        self._place_toast()
        super().resizeEvent(event)
