#
""""""

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt

from ..widgets.blur_overlay_label import BlurLabel
from ..utils.qt_objectname_utils import auto_set_object_names

from .titlebar import TitleBar
from .content import Content
from .album import Album
from .menu import Menu

from ..widgets.passthrough_stack_widget import PassthroughStack


class Window(QWidget):
    main_layout: QVBoxLayout

    back_ground: BlurLabel

    title_bar: TitleBar
    content: Content
    overlay: PassthroughStack
    album: Album
    menu: Menu

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup()
        self.setup_ui()
        auto_set_object_names(
            self,
            class_name_source=self,
            separator="",  # 去掉分隔符
            camel_case=True
        )

    def setup(self):
        self.setObjectName(type(self).__name__)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self.main_layout = layout
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        #
        self.back_ground = BlurLabel(self)

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

        #
        self.content = Content(self)
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.back_ground.target_widget = self.content

        #
        self.overlay = PassthroughStack(self)
        self.album = Album(self, self.overlay)
        self.overlay.addWidget(self.album)

        #
        self.menu = Menu(self)
        self.menu.exit_action.triggered.connect(self.close)
        self.title_bar.extra_button.setMenu(self.menu)

        #
        self.overlay.raise_()
        self.content.lower()
        self.back_ground.lower()
        layout.addWidget(self.title_bar)
        layout.addStretch()

    def _update_overlay_geometry(self):
        y = self.title_bar.action_bar_bottom_y_in_parent + 1
        self.overlay.setGeometry(
            0,
            y,
            self.width(),
            max(0, self.height() - y),
        )

    def resizeEvent(self, event):
        self.back_ground.setGeometry(self.rect())
        self.content.setGeometry(self.rect())
        self._update_overlay_geometry()
        super().resizeEvent(event)
