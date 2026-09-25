#
""""""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QPushButton,
    QVBoxLayout,
    QListView,
    QWidget,
    QHBoxLayout,
    QGridLayout,
)

from ..widgets import BlurLabel
from ..widgets.image_viewer import ImageViewer

from .utils import install_close_button


class Detail(BlurLabel):
    """照片详情页容器（继承自 BlurLabel，背景自动模糊）。"""
    close_requested = Signal()
    image_loaded = Signal(str)

    main_layout: QVBoxLayout

    close_button: QPushButton
    image_viewer: ImageViewer

    overlay: QWidget
    content_layout: QGridLayout
    item_line_viewer: QListView
    operation_container_widget: QWidget
    operation_container_layout: QHBoxLayout

    multiple_choice_button: QPushButton
    favourite_button: QPushButton
    information_button: QPushButton
    edit_button: QPushButton
    trash_button: QPushButton

    def __init__(self, parent=None):
        super().__init__(parent=parent, draw_label_content=False)
        self.setup_ui()
        self.setup_key()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        # ---------- 顶部：关闭按钮 ----------
        self.close_button = install_close_button(self)
        self.close_button.setObjectName("DetailCloseButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.main_layout.addWidget(self.close_button)

        # ---------- 内容区：图片铺满 + 底部叠加层 ----------
        content = QWidget(self)
        self.main_layout.addWidget(content, 1)  # stretch=1，吃掉剩余全部空间

        self.content_layout = QGridLayout(content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        # 第 1 层：图片视图，铺满整个 content
        self.image_viewer = ImageViewer(content)
        self.content_layout.addWidget(self.image_viewer, 0, 0)

        # 第 2 层：底部叠加层（导航栏 + 按钮行）
        self.overlay = QWidget(content)
        self.overlay.setObjectName("DetailOverlay")  # 想加半透明背景就给它写 QSS
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(12, 12, 12, 12)
        overlay_layout.setSpacing(8)

        # 导航栏（横向缩略图）
        self.item_line_viewer = QListView(self.overlay)
        self.item_line_viewer.setFlow(QListView.Flow.LeftToRight)
        self.item_line_viewer.setWrapping(False)
        self.item_line_viewer.setUniformItemSizes(True)
        self.item_line_viewer.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.item_line_viewer.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # 按钮
        self.multiple_choice_button = QPushButton(self.tr("choice"), self.overlay)
        self.favourite_button = QPushButton(self.tr("favourite"), self.overlay)
        self.information_button = QPushButton(self.tr("info"), self.overlay)
        self.edit_button = QPushButton(self.tr("edit"), self.overlay)
        self.trash_button = QPushButton(self.tr("trash"), self.overlay)

        # 三个按钮装进容器
        self.operation_container_widget = QWidget(self.overlay)
        self.operation_container_layout = QHBoxLayout(self.operation_container_widget)
        self.operation_container_layout.setContentsMargins(0, 0, 0, 0)
        self.operation_container_layout.setSpacing(8)
        self.operation_container_layout.addWidget(self.favourite_button)
        self.operation_container_layout.addWidget(self.information_button)
        self.operation_container_layout.addWidget(self.edit_button)

        # 底部按钮行：[choice] --- [容器] --- [trash]
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 0, 0, 0)
        bottom_bar.setSpacing(8)
        bottom_bar.addWidget(self.multiple_choice_button)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.operation_container_widget)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.trash_button)

        overlay_layout.addWidget(self.item_line_viewer)
        overlay_layout.addLayout(bottom_bar)

        # 关键：叠加层放在同一个 (0,0) 单元格，只占它自己的高度，贴底对齐
        self.content_layout.addWidget(
            self.overlay, 0, 0, Qt.AlignmentFlag.AlignBottom
        )

    def setup_key(self):
        # # ---------- Esc 关闭 ----------
        # sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        # sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        # sc.activated.connect(self.close_button.clicked.emit)
        pass

    def set_image(self, path: str | Path) -> bool:
        """接收图片路径（可直接 connect 到携带 str 的信号）。"""
        pixmap = QPixmap()
        path = str(path)
        ok = bool(path) and pixmap.load(path)
        self.image_viewer.set_pixmap(pixmap if ok else QPixmap())
        if ok:
            self.image_loaded.emit(path)
        return ok

    def clear_image(self) -> None:
        self.image_viewer.clear()

    # alias
    show_image = set_image
