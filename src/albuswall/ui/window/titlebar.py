#
"""标题栏：窗口动作按钮 + 工具按钮 + 浮动搜索栏 + 空白区事件透传。

纯视图组件：

* 文案集中在 :meth:`TitleBar.retranslate_ui`；
* 操作栏左侧的窗口标题镜像顶层窗口的 ``windowTitle``：改窗口标题就会
  自动更新（:meth:`Window.setWindowTitle <albuswall.ui.window.window.Window.setWindowTitle>`），
  工具栏的主标题 / 副标题是**另一套**业务文案，两者互不影响；
* 对外只暴露「设置显示什么」的方法与外发信号，不查询任何数据；
* 标题栏的空白区域把鼠标 / 滚轮事件转发给 :class:`~albuswall.ui.window.content.Content`，
  让模糊层上的滑动仍然作用于内容列表。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QIcon, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..anims.search_bar_anim import GeometryAnimator
from ..anims.search_bar_anim_protocol import SearchAnimator
from ..i18n import Retranslatable
from ..widgets.action_buttons import (
    CloseButton, MaximizeButton, MinimizeButton)
from ..widgets.floating_search_bar import (
    FloatingSearchBar,
    SearchBarLayoutPlaceholder,
)

__all__ = ["TitleBar"]

#: 工具栏三个按钮的名字（与 ``set_button`` 的 ``name`` 参数一致）。
_BUTTONS = ("album", "search", "extra")


class TitleBar(Retranslatable, QWidget):
    """标题栏。

    结构（自上而下）::

        action_bar   窗口动作条：窗口标题 + 最小化 / 最大化 / 关闭
        tool_bar     工具栏：图标 + 标题 + 副标题，右侧专辑 / 搜索 / 更多按钮
    """

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    #: 操作栏高度变化（内容层据此让出顶部空间）
    height_size_changed = Signal(int)

    # ---- 子控件声明（类型注解） ----
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

    album_button: QPushButton
    search_button: QPushButton
    extra_button: QPushButton

    floating_search_bar: FloatingSearchBar

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._passthrough_target: QWidget | None = None
        self._is_expanded: bool = False

        # 业务显式设过的按钮内容：切换语言时不被默认文案覆盖
        self._explicit_text: dict[str, str] = {}
        self._icon_only: set[str] = set()

        # 标题栏只占自身内容高度：内部工具栏里的弹簧会把多余高度撑开，
        # 所以这里显式声明「高度取 sizeHint，不随父布局增长」。
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

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

        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setScaledContents(False)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_and_icon_layout.addWidget(self.title_label)
        self.title_and_icon_layout.addWidget(self.sub_title)
        self.title_and_icon_layout.addWidget(self.icon_label)

        self.album_button = QPushButton(self.tool_bar)
        self.search_button = SearchBarLayoutPlaceholder(self.tool_bar)
        self.extra_button = QPushButton(self.tool_bar)

        for button in (self.album_button, self.search_button,
                       self.extra_button):
            button.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.tool_bar_holder_layout.addWidget(self.album_button)
        self.tool_bar_holder_layout.addStretch()
        self.tool_bar_holder_layout.addWidget(self.search_button)
        self.tool_bar_holder_layout.addWidget(self.extra_button)

        self.tool_bar_sub_layout.addLayout(self.title_and_icon_layout)
        self.tool_bar_sub_layout.addWidget(
            self.tool_bar_holder, alignment=Qt.AlignmentFlag.AlignTop)

        self.tool_bar_layout.addLayout(self.tool_bar_sub_layout)
        self.tool_bar_layout.addStretch(1)

        self.main_layout.addWidget(self.action_bar)
        self.main_layout.addWidget(self.tool_bar)

        # ---------------- 浮动搜索栏 ----------------
        # 父控件与 target 都是 tool_bar_holder；anchor 是 search_button。
        self.floating_search_bar = FloatingSearchBar(
            widget_parent=self.tool_bar_holder,
            target=self.tool_bar_holder,
            placeholder=self.search_button,
        )
        self.floating_search_bar.set_siblings_to_hide([
            self.album_button,
            self.search_button,
            self.extra_button,
        ])

        self.floating_search_bar.set_animator(
            GeometryAnimator(self.floating_search_bar))

        self.floating_search_bar.opened.connect(self._on_search_bar_opened)
        self.floating_search_bar.closed.connect(self._on_search_bar_closed)

    # ------------------------------------------------------------------ #
    # 对外接口：设置显示内容
    # ------------------------------------------------------------------ #
    def set_window_title(self, text: str | None) -> None:
        """操作栏左侧的窗口标题（空串表示隐藏）。

        这个标签**不是**业务文案：它镜像顶层窗口的 ``windowTitle``，由
        :class:`~albuswall.ui.window.window.Window` 在窗口标题改变时自动写入
        （见 ``Window.setWindowTitle`` / ``Window.changeEvent``）。外部一般
        不用自己调，改窗口标题即可。
        """
        if text is None:
            return
        title = str(text)
        self.window_title.setText(title)
        self.window_title.setVisible(title != "")

    def set_title(self, text: str | None) -> None:
        """工具栏主标题（空串表示隐藏）。"""
        if text is not None:
            title = str(text)
            self.title_label.setText(title)
            self.title_label.setVisible(title != "")

    def set_description(self, text: str | None) -> None:
        """工具栏副标题（空串表示隐藏）。"""
        if text is not None:
            description = str(text)
            self.sub_title.setText(description)
            self.sub_title.setVisible(description != "")

    def set_icon(self, icon: str | Path | QIcon | None) -> None:
        """工具栏封面图标；``None`` / 空串表示清空。"""
        if icon is None or str(icon) == "":
            self.icon_label.clear()
            self.icon_label.setVisible(False)
            return
        if isinstance(icon, QIcon):
            pixmap = icon.pixmap(self.icon_label.size())
        elif isinstance(icon, Path) or (isinstance(icon, str)
                                        and Path(icon).is_file()):
            pixmap = QIcon(str(icon)).pixmap(self.icon_label.size())
        else:
            pixmap = QIcon(str(icon)).pixmap(self.icon_label.size())
        if pixmap.isNull():
            self.icon_label.clear()
            self.icon_label.setVisible(False)
            return
        self.icon_label.setPixmap(pixmap)
        self.icon_label.setVisible(True)

    def set_button(
            self,
            name: str,
            *,
            text: str | None = None,
            icon: str | Path | None = None,
    ) -> None:
        """设置工具栏按钮的展示内容。

        :param name: ``"album"`` / ``"search"`` / ``"extra"``
        :param text: 文本；``None`` 表示沿用默认文案
        :param icon: 图标路径；给出图标时只显示图标

        显式给出的 ``text`` / ``icon`` 视为业务覆盖：之后切换语言时
        :meth:`retranslate_ui` 不会把它改回默认文案。
        """
        button = self._button(name)
        if button is None:
            return
        if icon:
            # 图标模式：只留图标，且不受语言切换影响；文案落到 tooltip
            self._icon_only.add(name)
            self._explicit_text.pop(name, None)
            button.setIcon(QIcon(str(icon)))
            button.setText("")
            button.setToolTip(self._button_text(name))
        else:
            self._icon_only.discard(name)
            button.setIcon(QIcon())
            if text:
                self._explicit_text[name] = str(text)
            else:
                self._explicit_text.pop(name, None)
            button.setText(self._button_text(name))
        button.setVisible(bool(button.text()) or not button.icon().isNull())

    def set_search_expanded(self, expanded: bool) -> None:
        """展开 / 收起浮动搜索栏。"""
        if expanded:
            self.floating_search_bar.expand()
        else:
            self.floating_search_bar.collapse()

    def set_animator(self, animator: SearchAnimator | None) -> None:
        """给搜索栏注入 / 替换动画策略（``None`` = 无动画）。"""
        self.floating_search_bar.set_animator(animator)

    def _button(self, name: str) -> QPushButton | None:
        return {
            "album": self.album_button,
            "search": self.search_button,
            "extra": self.extra_button,
        }.get(name)

    def _default_text(self, name: str) -> str:
        """按钮默认文案。

        源文写中文（默认语言是 zh_CN，不装翻译器也可读）；这里保持字面量
        写法 —— ``pyside6-lupdate`` 只提取字面量 ``tr("...")``。
        """
        if name == "album":
            return self.tr("相册")
        if name == "search":
            return self.tr("搜索")
        if name == "extra":
            return self.tr("更多")
        return ""

    def _button_text(self, name: str) -> str:
        """按钮当前应显示的文本：业务显式文本 > 词典译文 > 中文默认文案。"""
        explicit = self._explicit_text.get(name)
        if explicit is not None:
            return explicit
        return self.tr(self._default_text(name))

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用工具栏按钮文案（幂等：每次切换语言都重算）。

        图标模式的按钮不改文字，只同步 tooltip —— 图标不参与翻译，
        但提示文案要跟着语言走。
        """
        for name in _BUTTONS:
            button = self._button(name)
            if button is None:
                continue
            if name in self._icon_only:
                button.setToolTip(self._button_text(name))
                continue
            button.setText(self._button_text(name))
            button.setVisible(
                bool(button.text()) or not button.icon().isNull())

    def button_icon_mode(self, name: str) -> bool:
        """该工具按钮当前是否是「只有图标」模式。"""
        return name in self._icon_only

    # ------------------------------------------------------------------ #
    # 几何
    # ------------------------------------------------------------------ #
    @property
    def action_bar_bottom_y_in_parent(self) -> int:
        """操作栏下沿在父控件坐标系中的 y。"""
        parent = self.parentWidget()
        if parent is None:
            return 0
        return self.action_bar.mapTo(
            parent, self.action_bar.rect().bottomLeft()).y()

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        last_height = self.height()
        super().resizeEvent(event)
        if last_height != self.height():
            self.height_size_changed.emit(self.height())

    # ------------------------------------------------------------------ #
    # 空白区域事件透传
    # ------------------------------------------------------------------ #
    def set_passthrough_target(self, target: QWidget | None) -> None:
        """设置空白区域鼠标 / 滚轮事件的接收者（通常是 Content）。"""
        self._passthrough_target = target
        if target is None:
            return
        # 给自身和覆盖整块区域的容器都装过滤器
        for widget in (self, self.action_bar, self.tool_bar,
                       self.tool_bar_holder):
            widget.installEventFilter(self)

    def _hits_interactive(self, pos: QPoint) -> bool:
        """pos 是否落在真正需要交互的控件上。"""
        widget = self.childAt(pos)
        while widget is not None and widget is not self:
            if isinstance(widget, (QAbstractButton, FloatingSearchBar)):
                return True
            widget = widget.parentWidget()
        return False

    def _forward_event(self, event: QMouseEvent | QWheelEvent) -> bool:
        """空白区域的事件转发给目标；返回 ``True`` 表示已处理。"""
        target = self._passthrough_target
        if target is None or not target.isVisible():
            return False

        pos = event.position().toPoint()
        if not self.rect().contains(pos):
            return False
        if self._hits_interactive(pos):
            return False  # 落在按钮上，走正常流程

        target_pos = target.mapFromGlobal(self.mapToGlobal(pos))
        forwarded = self._clone_event(event, target_pos)
        if forwarded is None:
            return False
        QApplication.sendEvent(target, forwarded)
        return True

    @staticmethod
    def _clone_event(
            event: QMouseEvent | QWheelEvent,
            pos: QPoint | QPointF,
    ) -> QMouseEvent | QWheelEvent | None:
        """复制一份坐标已换算的事件；不支持的类型返回 ``None``。"""
        event_type = event.type()
        if event_type in (QEvent.Type.MouseButtonPress,
                          QEvent.Type.MouseButtonRelease,
                          QEvent.Type.MouseButtonDblClick):
            return QMouseEvent(
                event_type, pos, event.globalPosition(),
                event.button(), event.buttons(), event.modifiers(),
            )
        if event_type == QEvent.Type.MouseMove:
            return QMouseEvent(
                event_type, pos, event.globalPosition(),
                Qt.MouseButton.NoButton, event.buttons(), event.modifiers(),
            )
        if event_type == QEvent.Type.Wheel:
            return QWheelEvent(
                pos, event.globalPosition(),
                event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(),
                event.phase(), event.inverted(),
            )
        return None

    # ---------- 事件 ----------
    def mousePressEvent(self, event):  # noqa: N802 - Qt 命名
        if not self._forward_event(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt 命名
        if not self._forward_event(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 命名
        if not self._forward_event(event):
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - Qt 命名
        if not self._forward_event(event):
            super().wheelEvent(event)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        target = self._passthrough_target
        if (target is not None
                and event.type() == QEvent.Type.Wheel):
            global_pos = event.globalPosition().toPoint()
            local = self.mapFromGlobal(global_pos)

            # 1) 必须在标题栏范围内
            # 2) 不能落在按钮 / 搜索栏上
            if self.rect().contains(local) and not self._hits_interactive(local):
                new_event = QWheelEvent(
                    target.mapFromGlobal(global_pos),
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

    # ---------- 搜索栏状态（仅供外部查询） ----------
    def _on_search_bar_opened(self) -> None:
        self._is_expanded = True

    def _on_search_bar_closed(self) -> None:
        self._is_expanded = False

    @property
    def is_search_expanded(self) -> bool:
        return self._is_expanded
