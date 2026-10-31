#
"""相册页：整页模糊背景 + 可滚动的内容区（悬浮操作行 + 网格 + 更多视图）。

纯视图组件：不加载数据、不认识 model 的具体内容；外部把 model 塞给
``view`` / ``more_view`` 即可。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Retranslatable
from ..widgets.blur_overlay_label import BlurLabel
from ..widgets.square_grid import SquareGridView

from .utils import install_close_button

__all__ = ["Album"]


class Album(Retranslatable, BlurLabel):
    """相册页容器。"""

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout
    close_button: QPushButton

    root: QWidget
    root_layout: QVBoxLayout
    scroll_area: QScrollArea
    content: QWidget
    content_layout: QVBoxLayout

    hover: QFrame
    hover_layout: QVBoxLayout
    option_layout: QHBoxLayout
    edit_button: QPushButton
    add_button: QPushButton
    view: SquareGridView
    more_view: QListView

    def __init__(self,
                 target: QWidget | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent, target=target, draw_label_content=False)

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)

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
        self.scroll_area.setWidget(self.content)

        self.root_layout.addWidget(self.scroll_area)

        # ---- 悬浮操作行 + 网格 ----
        self.hover = QFrame(self.content)
        self.hover_layout = QVBoxLayout(self.hover)
        self.hover_layout.setSpacing(0)
        self.hover_layout.setContentsMargins(0, 0, 0, 0)

        self.option_layout = QHBoxLayout()

        self.edit_button = QPushButton("", self.hover)
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

        self.main_layout.addWidget(self.close_button)
        self.main_layout.addWidget(self.root)

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用文案。"""
        self.edit_button.setText(self.tr("编辑"))
