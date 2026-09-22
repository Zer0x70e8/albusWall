#
""""""

from typing import Optional, Dict, List

from PySide6.QtCore import Qt, Signal, QRect, QPoint, QEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QLineEdit, QPushButton, QComboBox
)

from ..anims.search_bar_anim_protocol import SearchAnimator
from ..utils.qt_objectname_utils import auto_set_object_names


class SearchBarLayoutPlaceholder(QPushButton):
    """锚点按钮。始终保留在布局中，展开时被搜索栏覆盖。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchPlaceholder")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class FloatingSearchBar(QFrame):
    """
    独立的浮动搜索栏组件。

    参数
    ----
    widget_parent : 搜索栏的父控件；决定坐标系。搜索栏会浮动在它之上。
    target        : 展开后覆盖的控件；提供展开态的宽 / 横坐标。
    placeholder   : 锚点按钮；点击它展开，其几何决定折叠态位置。

    公开接口
    --------
    信号 : opened / closed / search(dict)
    方法 : expand() / collapse() / toggle()
           set_animator(animator or None)
           set_siblings_to_hide([...])
    """

    opened = Signal()
    closed = Signal()
    search = Signal(dict)

    def __init__(self,
                 widget_parent: QWidget,
                 target: QWidget,
                 placeholder: QPushButton):
        super().__init__(widget_parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("SearchBar")

        self._widget_parent = widget_parent
        self._target = target
        self._placeholder = placeholder

        self._animator: Optional[SearchAnimator] = None
        self._siblings_to_hide: List[QWidget] = []
        self._saved_visibility: Dict[QWidget, bool] = {}
        self._locked_target_height: Optional[int] = None

        self.is_expanded = False

        self._setup_ui()

        auto_set_object_names(
            self,
            class_name_source=self,
            separator="",
            camel_case=True,
            overwrite=True
        )

        # 锚点被点击 → 展开
        placeholder.clicked.connect(self.expand)
        # target 尺寸变化 → 跟随
        target.installEventFilter(self)

        self.hide()

    # ---------------------------------------------------------------- UI
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(self.tr("search..."))
        self.line_edit.setObjectName("SearchLineEdit")

        self.option_combo = QComboBox()
        self.option_combo.setObjectName("SearchOptionCombo")
        if __debug__:
            self.option_combo.addItems(["all", "test1", "test2"])

        self.search_btn = QPushButton("🔍")
        self.search_btn.setObjectName("SearchSearchBtn")

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("SearchCloseBtn")
        self.close_btn.clicked.connect(self.collapse)

        layout.addWidget(self.line_edit)
        layout.addWidget(self.option_combo)
        layout.addWidget(self.search_btn)
        layout.addWidget(self.close_btn)

        self.line_edit.returnPressed.connect(self._trigger_search)
        self.search_btn.clicked.connect(self._trigger_search)

    def _trigger_search(self):
        self.search.emit({
            "text": self.line_edit.text().strip(),
            "option": self.option_combo.currentText(),
        })

    # ------------------------------------------------------------ 配置
    def set_animator(self, animator: Optional[SearchAnimator]) -> None:
        """注入/替换动画策略。None 表示无动画。"""
        if self._animator is not None:
            self._animator.cancel()
        self._animator = animator

    def set_siblings_to_hide(self, widgets: List[QWidget]) -> None:
        """展开期间要被隐藏的控件（折叠完成后自动恢复）。"""
        self._siblings_to_hide = [w for w in widgets if w is not self]

    # -------------------------------------------------------- 几何换算
    def _rect_in_parent(self, w: QWidget) -> QRect:
        """把控件 w 的几何换算到 widget_parent 坐标系。"""
        tl = w.mapTo(self._widget_parent, QPoint(0, 0))
        return QRect(tl, w.size())

    def _placeholder_rect(self) -> QRect:
        return self._rect_in_parent(self._placeholder)

    def _expanded_rect(self) -> QRect:
        """展开后矩形：横向对齐 target，纵向对齐 placeholder。"""
        t = self._rect_in_parent(self._target)
        ph = self._placeholder_rect()
        return QRect(t.x(), ph.y(), t.width(), ph.height())

    # -------------------------------------------------------- 展开/折叠
    def expand(self):
        if self.is_expanded:
            return
        self.is_expanded = True

        start = self._placeholder_rect()
        end = self._expanded_rect()

        # ★ 关键：在隐藏兄弟控件之前锁定 target 高度，
        #   否则布局会因控件被 hide 而收缩，标题栏高度会跳变。
        self._lock_target_height()

        # 隐藏指定兄弟（记录原状态）
        for w in self._siblings_to_hide:
            # if w is self._placeholder:
            #     continue
            if w not in self._saved_visibility:
                self._saved_visibility[w] = w.isVisible()
            w.hide()

        self.setGeometry(start)
        self.show()
        self.raise_()

        self.opened.emit()
        self._start_transition(start, end, expanding=True)

    def collapse(self):
        if not self.is_expanded:
            return
        self.is_expanded = False

        start = self.geometry()
        end = self._placeholder_rect()

        self._start_transition(start, end, expanding=False)

    def toggle(self):
        (self.collapse if self.is_expanded else self.expand)()

    def _lock_target_height(self) -> None:
        """在隐藏兄弟控件前把 target 高度固定住，避免布局收缩。"""
        if self._locked_target_height is not None:
            return
        h = self._target.height()
        if h <= 0:
            return
        self._locked_target_height = h
        self._target.setFixedHeight(h)

    def _unlock_target_height(self) -> None:
        """恢复 target 高度的自由度。"""
        if self._locked_target_height is None:
            return
        self._locked_target_height = None
        self._target.setMinimumHeight(0)
        self._target.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX

    # -------------------------------------------------------- 过渡调度
    def _start_transition(self, start: QRect, end: QRect, expanding: bool):
        if self._animator is None or start == end:
            self.setGeometry(end)
            self._on_transition_finished(expanding)
            return

        cb = lambda: self._on_transition_finished(expanding)
        if expanding:
            self._animator.start_expand(start, end, cb)
        else:
            self._animator.start_collapse(start, end, cb)

    def _on_transition_finished(self, expanding: bool):
        if expanding:
            return  # 展开完成：无需额外动作

        self.hide()
        for w, vis in self._saved_visibility.items():
            w.setVisible(vis)
        self._saved_visibility.clear()

        # ★ 先恢复兄弟控件的可见性，再解除高度约束，
        #   这样解锁的一瞬间布局已经回到正确尺寸，不会闪。
        self._unlock_target_height()

        self.closed.emit()

    # ------------------------------------------------------- resize 跟随
    def eventFilter(self, obj, event):
        if obj is self._target and event.type() == QEvent.Type.Resize:
            self._on_target_resized()
        return super().eventFilter(obj, event)

    def _on_target_resized(self):
        if not self.is_expanded or not self.isVisible():
            return
        new_rect = self._expanded_rect()
        if self._animator is not None:
            self._animator.update_target(new_rect)
        else:
            self.setGeometry(new_rect)
