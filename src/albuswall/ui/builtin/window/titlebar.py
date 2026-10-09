#
""""""

from pathlib import Path
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt, QPoint, QEvent, QPointF, Signal
from PySide6.QtGui import QIcon, QMouseEvent, QWheelEvent  # , QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QAbstractButton, QApplication)

from ..widgets.action_buttons import (
    CloseButton, MaximizeButton, MinimizeButton)
from ..widgets.floating_search_bar import (
    FloatingSearchBar,
    SearchBarLayoutPlaceholder,
)
from ...vo.album import TitleBarVO


class TitleBar(QWidget):
    height_size_changed = Signal(int)

    main_layout: QVBoxLayout

    action_bar: QWidget
    action_bar_layout: QHBoxLayout

    window_title: QLabel

    minimize_button: MinimizeButton
    maximize_button: MaximizeButton
    close_button: CloseButton

    tool_bar: QWidget
    tool_bar_layout: QVBoxLayout
    tool_bar_sub_layout: QHBoxLayout
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

        self._passthrough_target = None

        self._vo: TitleBarVO = TitleBarVO()
        self._default_texts: dict[str, str] = {
            "album": self.tr("album"),
            "extra": self.tr("extra"),
            "search": self.tr("search"),
        }

        self.setup_ui()

        # # self.floating_search_bar.setObjectName("SearchBar")
        #
        # from PySide6.QtCore import QTimer
        # QTimer.singleShot(50, lambda: print(self.extra_button.objectName()))

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
        self.tool_bar_layout = QVBoxLayout(self.tool_bar)
        self.tool_bar_sub_layout = QHBoxLayout()
        self.tool_bar_sub_layout.setContentsMargins(0, 0, 0, 0)
        self.tool_bar_sub_layout.setSpacing(0)

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

        self.tool_bar_sub_layout.addLayout(self.title_and_icon_layout)
        self.tool_bar_sub_layout.addWidget(
            self.tool_bar_holder,
            alignment=Qt.AlignmentFlag.AlignTop
        )

        self.tool_bar_layout.addLayout(self.tool_bar_sub_layout)
        self.tool_bar_layout.addStretch(1)

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

        # # 三个按钮：图标和文本分开处理，互不干扰
        # self._apply_icon(self.album_button, vo.album_icon)
        # self._apply_icon(self.extra_button, vo.extra_icon)
        # self._apply_icon(self.search_button, vo.search_icon)
        #
        # self._apply_text(self.album_button, vo.album_text)
        # self._apply_text(self.extra_button, vo.extra_text)
        # self._apply_text(self.search_button, vo.search_text)
        #
        # # 按钮显隐交给渲染层决定：这里只保证"有图标或有文本"的可见性
        # for btn in (self.album_button, self.extra_button, self.search_button):
        #     has_content = bool(btn.icon().isNull()) is False or bool(btn.text())
        #     btn.setVisible(has_content)

        self._apply_button(
            self.album_button, vo.album_icon, vo.album_text, "album")
        self._apply_button(
            self.extra_button, vo.extra_icon, vo.extra_text, "extra")
        self._apply_button(
            self.search_button, vo.search_icon, vo.search_text, "search")

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

    def refresh_translations(self) -> None:
        self._default_texts = {
            "album": self.tr("album"),
            "extra": self.tr("extra"),
            "search": self.tr("search"),
        }
        self._apply_vo(self._vo)  # 重刷按钮文案

    def _apply_button(
            self,
            button: QPushButton,
            icon: Optional[Path],
            text: Optional[str],
            fallback_key: str,
    ) -> None:
        """渲染策略：

        - 有图标  -> 只显示图标（清空文本，避免"图标+文字"并排）
        - 无图标  -> 显示文本；文本为空则回退到 self._default_texts
        - 两者都空 -> 隐藏按钮（正常情况不会走到，仅作为兜底）
        """
        if icon is not None:
            button.setIcon(QIcon(str(icon)))
            button.setText("")
        else:
            # 显式清空图标，避免刷新时残留上一张图
            button.setIcon(QIcon())
            if text:
                button.setText(str(text))
            else:
                button.setText(self._default_texts.get(fallback_key, ""))

        has_content = (not button.icon().isNull()) or bool(button.text())
        button.setVisible(has_content)

    # def set_passthrough_target(self, target: QWidget | None) -> None:
    #     """设置空白区域鼠标 / 滚轮事件的接收者（通常是 Content）。"""
    #     self._passthrough_target = target

    def _is_interactive_at(self, pos: QPoint) -> bool:
        """判断 pos 是否落在真正可交互的控件上（按钮 / 搜索栏等）。"""
        w = self.childAt(pos)
        while w is not None and w is not self:
            if isinstance(w, QAbstractButton):
                return True
            if isinstance(w, FloatingSearchBar):
                return True
            if w is None:
                return True
            w = w.parentWidget()
        return False

    def _forward_event(self, event) -> bool:
        """如果是空白区域，就转发给目标；返回 True 表示已处理。"""
        target = self._passthrough_target
        if target is None or not target.isVisible():
            return False

        pos = event.position().toPoint()
        if not self.rect().contains(pos):
            return False
        if self._is_interactive_at(pos):
            return False  # 落在按钮上，走正常流程

        # target_pos = target.mapFrom(self, pos)
        target_pos = target.mapFromGlobal(self.mapToGlobal(pos))
        forwarded = self._clone_event(event, target_pos)
        if forwarded is None:
            return False
        QApplication.sendEvent(target, forwarded)
        return True

    @staticmethod
    def _clone_event(event, pos: QPoint | QPointF):
        et = event.type()
        if et in (QEvent.Type.MouseButtonPress,
                  QEvent.Type.MouseButtonRelease,
                  QEvent.Type.MouseButtonDblClick):
            return QMouseEvent(
                et, pos, event.globalPosition(),
                event.button(), event.buttons(), event.modifiers(),
            )
        if et == QEvent.Type.MouseMove:
            return QMouseEvent(
                et, pos, event.globalPosition(),
                Qt.MouseButton.NoButton, event.buttons(), event.modifiers(),
            )
        if et == QEvent.Type.Wheel:
            return QWheelEvent(
                pos, event.globalPosition(),
                event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(),
                event.phase(), event.inverted(),
            )
        return None

    # ---------- 重写事件：空白区域转发 ----------
    def mousePressEvent(self, event):
        if not self._forward_event(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._forward_event(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if not self._forward_event(event):
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if not self._forward_event(event):
            super().wheelEvent(event)

    def set_passthrough_target(self, target) -> None:
        """设置空白区域事件的接收者（通常是 Content）。"""
        self._passthrough_target = target
        # 给自身和覆盖整块区域的容器都装过滤器
        for w in (self, self.action_bar, self.tool_bar, self.tool_bar_holder):
            w.installEventFilter(self)

    def _hits_interactive(self, pos) -> bool:
        """pos 是否落在真正需要交互的控件上。"""
        w = self.childAt(pos)
        while w is not None and w is not self:
            if isinstance(w, (QAbstractButton, FloatingSearchBar)):
                return True
            w = w.parentWidget()
        return False

    def resizeEvent(self, event):
        last_height = self.height()
        super().resizeEvent(event)
        if last_height != self.height():
            self.height_size_changed.emit(self.height())

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.Wheel
                and getattr(self, "_passthrough_target", None) is not None):
            gp = event.globalPosition().toPoint()
            local = self.mapFromGlobal(gp)

            # 1) 必须在标题栏范围内
            # 2) 不能落在按钮 / 搜索栏上
            if self.rect().contains(local) and not self._hits_interactive(local):
                target = self._passthrough_target
                new_event = QWheelEvent(
                    target.mapFromGlobal(gp),
                    event.globalPosition(),
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                )
                QApplication.sendEvent(target, new_event)
                return True  # 拦截掉，避免 tool_bar 再处理

        return super().eventFilter(obj, event)
