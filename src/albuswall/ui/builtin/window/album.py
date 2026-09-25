#
""""""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QPushButton, QVBoxLayout, QSizePolicy, QFrame,
    QHBoxLayout, QListView, QScrollArea)

from ..widgets.blur_overlay_label import BlurLabel
from ..widgets.square_grid import SquareGridView
from ..utils.qt_objectname_utils import auto_set_object_names

from .utils import install_close_button


class Album(BlurLabel):
    main_layout: QVBoxLayout
    root: QWidget
    root_layout: QVBoxLayout
    scroll_area: QScrollArea
    content: QWidget
    content_layout: QVBoxLayout
    close_button: QPushButton

    hover: QFrame
    hover_layout: QVBoxLayout
    option_layout: QHBoxLayout
    edit_button: QPushButton
    add_button: QPushButton
    view: SquareGridView
    more_view: QListView

    def __init__(self,
                 target=None,
                 parent: QWidget | None = None):
        super().__init__(
            parent, target=target, draw_label_content=False)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self.main_layout = layout

        self.close_button = install_close_button(self)

        self.close_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.close_button.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True)

        self.root = QWidget(self)
        self.root_layout = QVBoxLayout(self.root)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)

        self.content_layout.setSpacing(0)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)
        self.root_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea(self.root)

        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)  # 去掉自带的边框
        self.scroll_area.viewport().setAutoFillBackground(False)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.content)
        # # self.scroll_area.viewport().setObjectName("AlbumScrollViewport")
        # from PySide6.QtCore import QTimer
        # QTimer.singleShot(100, lambda:
        # self.scroll_area.setStyleSheet("""
        # #AlbumScrollArea,
        # #AlbumScrollArea > QWidget,
        # #AlbumScrollArea > QWidget > QWidget,
        # #AlbumScrollViewport {background: transparent;}
        # """))
        # QTimer.singleShot(100, lambda: print(self.scroll_area.objectName()))

        self.root_layout.addWidget(self.scroll_area)

        self.hover = QFrame(self.content)
        self.hover_layout = QVBoxLayout(self.hover)
        self.hover_layout.setSpacing(0)
        self.hover_layout.setContentsMargins(0, 0, 0, 0)

        self.option_layout = QHBoxLayout()

        self.edit_button = QPushButton(self.tr("edit"), self.hover)
        self.add_button = QPushButton("+", self.hover)

        self.option_layout.addStretch()
        self.option_layout.addWidget(self.edit_button)
        self.option_layout.addWidget(self.add_button)

        self.view = SquareGridView(parent=self.content)

        self.hover_layout.addLayout(self.option_layout)
        self.hover_layout.addWidget(self.view)

        self.more_view = QListView(self.content)

        self.content_layout.addWidget(self.hover)
        self.content_layout.addWidget(self.more_view)
        self.content_layout.addStretch()

        layout.addWidget(self.close_button)
        layout.addWidget(self.root)

        auto_set_object_names(
            self,
            class_name_source=self,
            separator="",
            camel_case=True,
            overwrite=True
        )

        # from PySide6.QtCore import QTimer
        # QTimer.singleShot(
        #     50,
        #     lambda: print(self.close_button.objectName(), )
        # )
