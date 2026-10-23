#
""""""

from typing import Optional, Dict, List, Iterable, Tuple

from PySide6.QtCore import Qt, Signal, QRect, QPoint, QEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QLineEdit, QPushButton, QComboBox,
    QListWidget, QListWidgetItem, QVBoxLayout,
)

from ..anims.search_bar_anim_protocol import SearchAnimator
from ..i18n import Retranslatable
from ..utils.qt_objectname_utils import set_widget_object_name


class SearchBarLayoutPlaceholder(QPushButton):
    """锚点按钮。始终保留在布局中，展开时被搜索栏覆盖。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchPlaceholder")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class SearchSuggestionPopup(Retranslatable, QFrame):
    """搜索栏标签建议列表（普通子控件，不抢焦点）。

    刻意不用 ``Qt.WindowType.Popup``：Popup 会抢走 QLineEdit 的键盘焦点，
    用户打字时建议列表就会消失。这里用顶层窗口的普通子控件 +
    ``raise_()``，浮在搜索栏下方，键盘事件仍由 QLineEdit 处理。
    """

    chosen = Signal(str)

    ITEM_HEIGHT = 28
    MAX_VISIBLE = 8

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("SearchSuggestionPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.list = QListWidget(self)
        self.list.setObjectName("SearchSuggestionList")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list)

        # 最近一次的数据：语言切换时用它重新渲染（尤其是「无匹配」占位行）
        self._rows: List[Tuple[str, str]] = []

        self.hide()

    # ---- 数据 ----
    def set_items(self, items: Iterable[Tuple[str, str]]) -> None:
        """items: ``[(tag_uuid, label), ...]``；空列表显示无结果占位。"""
        self._rows = list(items)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """按缓存的数据重画列表（占位文案会随语言变化）。"""
        self._render(self._rows)

    def _render(self, rows: List[Tuple[str, str]]) -> None:
        self.list.clear()
        if not rows:
            item = QListWidgetItem(self.tr("无匹配标签"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        else:
            for uuid, label in rows:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, uuid)
                self.list.addItem(item)
            self.list.setCurrentRow(0)
        self.list.setFixedHeight(
            max(1, min(self.list.count(), self.MAX_VISIBLE))
            * self.ITEM_HEIGHT + 2
        )

    def clear(self) -> None:
        self.list.clear()

    # ---- 位置 ----
    def show_under(self, anchor: QWidget) -> None:
        parent = self.parentWidget()
        if parent is None or not anchor.isVisible():
            return
        top_left = anchor.mapTo(parent, QPoint(0, anchor.height()))
        width = max(anchor.width(), 260)
        height = max(self.ITEM_HEIGHT * 2, self.list.height() + 2)
        self.setGeometry(top_left.x(), top_left.y(), width, height)
        self.raise_()
        self.show()

    # ---- 键盘 ----
    def move_selection(self, delta: int) -> None:
        count = self.list.count()
        if count == 0:
            return
        row = self.list.currentRow()
        if row < 0:
            row = 0 if delta > 0 else count - 1
        else:
            row = max(0, min(count - 1, row + delta))
        self.list.setCurrentRow(row)
        self.list.scrollToItem(self.list.item(row))

    def current_uuid(self) -> Optional[str]:
        item = self.list.currentItem()
        if item is None:
            return None
        uuid = item.data(Qt.ItemDataRole.UserRole)
        return str(uuid) if uuid else None

    def activate_current(self) -> bool:
        uuid = self.current_uuid()
        if not uuid:
            return False
        self.chosen.emit(uuid)
        self.hide()
        return True

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        uuid = item.data(Qt.ItemDataRole.UserRole)
        if not uuid:
            return
        self.chosen.emit(str(uuid))
        self.hide()


class FloatingSearchBar(Retranslatable, QFrame):
    """独立的浮动搜索栏组件。

    参数
    ----
    widget_parent : 搜索栏的父控件；决定坐标系。搜索栏会浮动在它之上。
    target        : 展开后覆盖的控件；提供展开态的宽 / 横坐标。
    placeholder   : 锚点按钮；点击它展开，其几何决定折叠态位置。

    公开接口
    --------
    信号 : opened / closed / search(dict) / text_changed(str)
           mode_changed(str) / tag_chosen(str)
    方法 : expand() / collapse() / toggle()
           set_animator(animator or None)
           set_siblings_to_hide([...])
           set_suggestions([(uuid, label), ...]) / hide_suggestions()
    """

    opened = Signal()
    closed = Signal()
    search = Signal(dict)
    text_changed = Signal(str)
    mode_changed = Signal(str)
    tag_chosen = Signal(str)

    #: 模式用**稳定标识**，展示文本另取（语言切换不能改变标识）
    MODE_SIMPLE = "simple"
    MODE_COMMAND = "command"

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
        # 父控件用顶层窗口：建议列表要盖在 overlay 之上，而 title_bar 本身
        # 在 Window 的层级里可能低于 overlay。
        self._suggestions = SearchSuggestionPopup(widget_parent.window())
        self._suggestions.chosen.connect(self._on_suggestion_chosen)

        self.retranslate_ui()

        set_widget_object_name(
            self,
            parent_class_name=type(self).__name__,
            separator="",
            overwrite=True
        )

        # 锚点被点击 → 展开
        placeholder.clicked.connect(self.expand)
        # target 尺寸变化 → 跟随
        target.installEventFilter(self)
        self.line_edit.installEventFilter(self)

        self.hide()

    # ---------------------------------------------------------------- UI
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = QLineEdit()
        self.line_edit.setObjectName("SearchLineEdit")
        self.line_edit.setClearButtonEnabled(True)

        self.option_combo = QComboBox()
        self.option_combo.setObjectName("SearchOptionCombo")
        # 条目 = (展示文本, 稳定标识)；文本在 retranslate_ui() 里填
        self.option_combo.addItem("", self.MODE_SIMPLE)
        self.option_combo.addItem("", self.MODE_COMMAND)

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
        self.line_edit.textChanged.connect(self._on_text_changed)
        self.option_combo.currentIndexChanged.connect(self._on_mode_changed)

    def _placeholder_text(self) -> str:
        if self.mode == self.MODE_COMMAND:
            return self.tr("命令模式（未实现）")
        return self.tr("输入标签名筛选当前相册...")

    def _mode_text(self, mode: str) -> str:
        """模式标识 → 展示文本（唯一需要翻译的地方）。"""
        if mode == self.MODE_COMMAND:
            return self.tr("命令模式")
        return self.tr("简单模式")

    def retranslate_ui(self) -> None:
        """重新应用模式条目 / 输入提示 / 按钮文案，并保持当前选择。"""
        blocked = self.option_combo.blockSignals(True)
        try:
            current = self.option_combo.currentIndex()
            for index in range(self.option_combo.count()):
                mode = self.option_combo.itemData(index)
                if mode:
                    self.option_combo.setItemText(index, self._mode_text(mode))
            if current >= 0:
                self.option_combo.setCurrentIndex(current)
        finally:
            self.option_combo.blockSignals(blocked)

        self.line_edit.setPlaceholderText(self._placeholder_text())
        self.search_btn.setToolTip(self.tr("搜索"))
        self.close_btn.setToolTip(self.tr("关闭搜索栏"))

    def set_mode(self, mode: str) -> None:
        """按稳定标识切换模式（不依赖下拉框当前文本）。"""
        index = self.option_combo.findData(mode)
        if index >= 0:
            self.option_combo.setCurrentIndex(index)

    def _trigger_search(self):
        self.search.emit({
            "text": self.line_edit.text().strip(),
            "option": self.mode,   # 稳定标识，不随界面语言变化
        })

    # ------------------------------------------------------------ 建议列表
    def set_suggestions(self, items: Iterable[Tuple[str, str]]) -> None:
        """显示 / 更新标签建议；``items=[(uuid, label), ...]``。"""
        self._suggestions.set_items(items)
        self._suggestions.show_under(self.line_edit)

    def hide_suggestions(self) -> None:
        self._suggestions.hide()

    def _on_suggestion_chosen(self, uuid: str) -> None:
        self.tag_chosen.emit(uuid)

    def _on_text_changed(self, text: str) -> None:
        if not text.strip():
            self._suggestions.hide()
        self.text_changed.emit(text)

    def _on_mode_changed(self, _index: int) -> None:
        self._suggestions.hide()
        self.line_edit.setPlaceholderText(self._placeholder_text())
        self.mode_changed.emit(self.mode)

    @property
    def mode(self) -> str:
        """当前模式的稳定标识（``MODE_SIMPLE`` / ``MODE_COMMAND``）。"""
        value = self.option_combo.currentData()
        return str(value) if value else self.MODE_SIMPLE

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
        self.line_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.line_edit.selectAll()

        self.opened.emit()
        self._start_transition(start, end, expanding=True)

    def collapse(self):
        if not self.is_expanded:
            return
        self.is_expanded = False
        self._suggestions.hide()

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
        line_edit = getattr(self, "line_edit", None)
        if line_edit is not None and obj is line_edit \
                and event.type() == QEvent.Type.KeyPress:
            if self._handle_line_edit_key(event):
                return True
        target = getattr(self, "_target", None)
        if target is not None and obj is target \
                and event.type() == QEvent.Type.Resize:
            self._on_target_resized()
        return super().eventFilter(obj, event)

    def _handle_line_edit_key(self, event) -> bool:
        """建议列表可见时接管上下键 / 回车 / Esc；其余按键交给 QLineEdit。"""
        suggestions = getattr(self, "_suggestions", None)
        if suggestions is None or not suggestions.isVisible():
            return False
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            suggestions.move_selection(
                1 if key == Qt.Key.Key_Down else -1
            )
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return suggestions.activate_current()
        if key == Qt.Key.Key_Escape:
            suggestions.hide()
            return True
        return False

    def _on_target_resized(self):
        if not self.is_expanded or not self.isVisible():
            return
        new_rect = self._expanded_rect()
        if self._animator is not None:
            self._animator.update_target(new_rect)
        else:
            self.setGeometry(new_rect)
        if self._suggestions.isVisible():
            self._suggestions.show_under(self.line_edit)
