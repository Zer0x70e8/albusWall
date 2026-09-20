#
""""""

from pathlib import Path
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon  # , QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from ..utils.qt_objectname_utils import auto_set_object_names
from ..widgets.action_buttons import (
    CloseButton, MaximizeButton, MinimizeButton)
from ..widgets.floating_search_bar import (
    FloatingSearchBar,
    SearchBarLayoutPlaceholder,
)
from ...vo.album import TitleBarVO


class TitleBar(QWidget):
    main_layout: QVBoxLayout

    action_bar: QWidget
    action_bar_layout: QHBoxLayout

    window_title: QLabel

    minimize_button: MinimizeButton
    maximize_button: MaximizeButton
    close_button: CloseButton

    tool_bar: QWidget
    tool_bar_layout: QHBoxLayout
    tool_bar_holder: QWidget
    tool_bar_holder_layout: QHBoxLayout

    title_and_icon_layout: QVBoxLayout
    title_label: QLabel
    sub_title: QLabel
    icon_label: QLabel

    # 三个按钮都是 QPushButton 派生类，统一按 QPushButton 处理
    album_button: QPushButton
    search_button: QPushButton
    extra_button: QPushButton

    floating_search_bar: FloatingSearchBar

    def __init__(self, parent=None):
        super().__init__(parent)

        self._vo: TitleBarVO = TitleBarVO()

        self.setup_ui()

        auto_set_object_names(
            self,
            class_name_source=self,
            separator="",
            camel_case=True,
            overwrite=True
        )
        # self.floating_search_bar.setObjectName("SearchBar")

        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, lambda: print(self.extra_button.objectName()))

    @property
    def vo(self) -> TitleBarVO:
        return self._vo

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self.main_layout = layout

        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---------------- 操作栏 ----------------
        self.action_bar = QWidget(self)
        self.action_bar_layout = QHBoxLayout(self.action_bar)
        self.action_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.action_bar_layout.setSpacing(0)

        self.window_title = QLabel(self)

        self.minimize_button = MinimizeButton(self.action_bar)
        self.maximize_button = MaximizeButton(self.action_bar)
        self.close_button = CloseButton(self.action_bar)

        self.action_bar_layout.addWidget(self.window_title)
        self.action_bar_layout.addStretch()
        self.action_bar_layout.addWidget(self.minimize_button)
        self.action_bar_layout.addWidget(self.maximize_button)
        self.action_bar_layout.addWidget(self.close_button)

        # ---------------- 工具栏 ----------------
        self.tool_bar = QWidget(self)
        self.tool_bar_layout = QHBoxLayout(self.tool_bar)
        self.tool_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.tool_bar_layout.setSpacing(0)

        self.tool_bar_holder = QWidget(self.tool_bar)
        self.tool_bar_holder_layout = QHBoxLayout(self.tool_bar_holder)
        self.tool_bar_holder_layout.setContentsMargins(0, 0, 0, 0)
        self.tool_bar_holder_layout.setSpacing(0)

        self.title_and_icon_layout = QVBoxLayout()
        self.title_and_icon_layout.setContentsMargins(0, 0, 0, 0)
        self.title_and_icon_layout.setSpacing(0)

        self.title_label = QLabel(self.tool_bar)
        self.sub_title = QLabel(self.tool_bar)
        self.icon_label = QLabel(self.tool_bar)

        self.icon_label.setFixedSize(32, 32)  # 想更大可改成 48
        self.icon_label.setScaledContents(False)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_and_icon_layout.addWidget(self.title_label)
        self.title_and_icon_layout.addWidget(self.sub_title)
        self.title_and_icon_layout.addWidget(self.icon_label)

        self.album_button = QPushButton(self.tool_bar)
        self.search_button = SearchBarLayoutPlaceholder(self.tool_bar)
        self.extra_button = QPushButton(self.tool_bar)

        for btn in (self.album_button, self.search_button, self.extra_button):
            btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.tool_bar_holder_layout.addWidget(self.album_button)
        self.tool_bar_holder_layout.addStretch()
        self.tool_bar_holder_layout.addWidget(self.search_button)
        self.tool_bar_holder_layout.addWidget(self.extra_button)

        self.tool_bar_layout.addLayout(self.title_and_icon_layout)
        self.tool_bar_layout.addWidget(self.tool_bar_holder)

        layout.addWidget(self.action_bar)
        layout.addWidget(self.tool_bar)

        # ---------------- 浮动搜索栏 ----------------
        # 父控件与 target 都是 tool_bar；anchor 是 search_button。
        self.floating_search_bar = FloatingSearchBar(
            widget_parent=self.tool_bar_holder,
            target=self.tool_bar_holder,
            placeholder=self.search_button,
        )
        # 展开时被隐藏的兄弟控件（不含 placeholder 本身）
        self.floating_search_bar.set_siblings_to_hide([
            # self.title_label,
            # self.icon_label,
            # self.sub_title,
            self.album_button,
            self.search_button,
            self.extra_button,
        ])

        # 需要动画时打开下面两行（无动画时可以完全不写）
        from ..anims.search_bar_anim import GeometryAnimator
        self.floating_search_bar.set_animator(
            GeometryAnimator(self.floating_search_bar)
        )

    def set_vo(self, vo: TitleBarVO | Mapping[str, Any] | None) -> None:
        """绑定（或替换）视图数据，并立即刷新界面。"""
        if vo is None:
            vo = TitleBarVO()
        elif not isinstance(vo, TitleBarVO):
            vo = TitleBarVO.from_dict(vo)

        self._vo = vo
        self._apply_vo(vo)

    # ---------------- 内部渲染 ----------------

    @staticmethod
    def _apply_icon(button: QPushButton, icon: Optional[Path]) -> None:
        """图标字段：None 跳过；Path 设置图标（不触碰文本）。"""
        if icon is None:
            return
        button.setIcon(QIcon(str(icon)))

    @staticmethod
    def _apply_text(button: QPushButton, text: Optional[str]) -> None:
        """文本字段：None 跳过；str 设置文本（不触碰图标）。"""
        if text is None:
            return
        button.setText(str(text))

    def _apply_vo(self, vo: TitleBarVO) -> None:
        # window_title：纯文本字段
        if vo.window_title is not None:
            self.window_title.setText(str(vo.window_title))

        # # cover：窗口图标路径。
        # # 空 / 非文件 -> 隐藏 icon_label，避免 QPixmap 画出损坏图
        # if vo.cover is not None:
        #     icon_str = str(vo.cover)
        #     icon_path = Path(icon_str) if icon_str else None
        #     if icon_path is not None and icon_path.is_file():
        #         pixmap = QPixmap(str(icon_path))
        #         if not pixmap.isNull():
        #             # 等比缩放到 icon_label 的当前尺寸，平滑插值
        #             pixmap = pixmap.scaled(
        #                 self.icon_label.size(),
        #                 Qt.AspectRatioMode.KeepAspectRatio,
        #                 Qt.TransformationMode.SmoothTransformation,
        #             )
        #             self.icon_label.setPixmap(pixmap)
        #             self.icon_label.setVisible(True)
        #         else:
        #             self.icon_label.clear()
        #             self.icon_label.setVisible(False)
        #     else:
        #         self.icon_label.clear()
        #         self.icon_label.setVisible(False)

        # title：纯文本字段
        if vo.title is not None:
            title_str = str(vo.title)
            self.title_label.setText(title_str)
            self.title_label.setVisible(title_str != "")

        if vo.description is not None:
            description = vo.description
            self.sub_title.setText(description)
            self.sub_title.setVisible(description != "")

        # 三个按钮：图标和文本分开处理，互不干扰
        self._apply_icon(self.album_button, vo.album_icon)
        self._apply_icon(self.extra_button, vo.extra_icon)
        self._apply_icon(self.search_button, vo.search_icon)

        self._apply_text(self.album_button, vo.album_text)
        self._apply_text(self.extra_button, vo.extra_text)
        self._apply_text(self.search_button, vo.search_text)

        # 按钮显隐交给渲染层决定：这里只保证"有图标或有文本"的可见性
        for btn in (self.album_button, self.extra_button, self.search_button):
            has_content = bool(btn.icon().isNull()) is False or bool(btn.text())
            btn.setVisible(has_content)

        if vo.search_expanded is not None:
            (self.floating_search_bar.expand
             if vo.search_expanded
             else self.floating_search_bar.collapse)()

    def set_animator(self, animator) -> None:
        """给搜索栏注入 / 替换动画策略（None = 无动画）。"""
        self.floating_search_bar.set_animator(animator)

    @property
    def action_bar_bottom_y_in_parent(self) -> int:
        parent = self.parentWidget()
        if parent is None:
            return 0
        return self.action_bar.mapTo(
            parent,
            self.action_bar.rect().bottomLeft()
        ).y()
