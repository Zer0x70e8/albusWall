#
"""标签栏：chip 列表 + 输入框 + 标签树弹窗。

纯视图组件：

* chip 列表由外部注入 model（外部只需提供 ``DisplayRole`` 文本与
  :data:`~albuswall.ui.widgets.tag_chip_view.TAG_ROLE` 标识）；
* 标签树弹窗的 model 同样由外部注入；
* 所有用户动作都只发意图信号，不含任何增删改查。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Retranslatable
from ..model.roles import TagRole
from ..widgets import BlurLabel
from ..widgets.anchored_popup import AnchoredPopup
from ..widgets.tag_chip_view import TagChipView

__all__ = ["TagBar"]

#: 标签树模型里承载「标签标识」的 role（与 chip 列表保持一致）。
#: 单一来源在 ``albuswall.ui.model.roles.TagRole``，这里只是别名。
TREE_TAG_ROLE = int(TagRole.Tag)


def _tag_id(value: object) -> str:
    """从 role 数据里取标签标识：带 ``uuid`` 的对象或裸值都支持。"""
    if value is None:
        return ""
    uid = getattr(value, "uuid", value)
    return str(uid) if uid is not None else ""


class LayerTreePopup(AnchoredPopup):
    """标签树的浮层容器。"""

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout
    tree_view: QTreeView

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)

        self.tree_view = QTreeView(self)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setUniformRowHeights(True)

        self.main_layout.addWidget(self.tree_view)


class TagBar(Retranslatable, QFrame):
    """标签栏。"""

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    apply_requested = Signal(str)           # 输入框文本
    chip_activated = Signal(str)            # 标识
    chip_remove_requested = Signal(str)     # 标识
    layer_tree_open_requested = Signal()    # 弹出 / 关闭切换前
    layer_tree_activated = Signal(str)      # 标识
    tag_create_requested = Signal(str)      # 父标签标识；空串 = 根
    tag_rename_requested = Signal(str)      # 标识
    tag_delete_requested = Signal(str)      # 标识

    # ---- 子控件声明（类型注解） ----
    main_layout: QHBoxLayout
    content_widget: BlurLabel
    content_layout: QHBoxLayout

    tags_view: TagChipView

    tag_add_container_widget: QWidget
    tag_add_container_layout: QHBoxLayout
    tag_line_edit: QLineEdit
    tag_apply_button: QPushButton

    layer_tree_button: QPushButton
    layer_tree_popup: LayerTreePopup
    tag_create_button: QPushButton

    collapse_toggle_button: QPushButton

    # 折叠符号：默认是文本（">" / "<"），也可通过 config 注入图标
    expand_icon: str | QIcon | QPixmap
    collapse_icon: str | QIcon | QPixmap

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent=parent)

        self._is_collapse = False
        self._old_tag_add_container_widget: int | None = None
        self._popup_closed_at: float = 0.0
        self.expand_icon = ">"
        self.collapse_icon = "<"

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.content_widget = BlurLabel(self, draw_label_content=False)
        # 显式命名：样式表用 ``#TagBarContentWidget`` 命中这层模糊底
        # （``BlurLabel`` 构造期会先把自己命名成类名，不覆盖的话拿不到这个名）。
        self.content_widget.setObjectName("TagBarContentWidget")
        self.content_layout = QHBoxLayout(self.content_widget)
        self.main_layout = QHBoxLayout(self)
        self.content_widget.target_widget = self

        # ---- chip 列表 ----
        self.tags_view = TagChipView(self.content_widget)
        self.tags_view.setFlow(QListView.Flow.LeftToRight)
        self.tags_view.setWrapping(False)
        self.tags_view.setUniformItemSizes(False)  # chip 宽度不固定
        self.tags_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tags_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tags_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        # ---- 添加标签输入区 ----
        self.tag_add_container_widget = QWidget(self.content_widget)
        self.tag_add_container_layout = QHBoxLayout(
            self.tag_add_container_widget)
        self.tag_add_container_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_add_container_layout.setSpacing(0)

        self.tag_line_edit = QLineEdit(self.tag_add_container_widget)
        self.tag_apply_button = QPushButton(self.tag_add_container_widget)
        self.tag_add_container_layout.addWidget(self.tag_line_edit)
        self.tag_add_container_layout.addWidget(self.tag_apply_button)

        # ---- 标签树 ----
        self.layer_tree_button = QPushButton("", self.content_widget)
        self.layer_tree_button.clicked.connect(self._on_toggle_layer_tree_popup)

        self.tag_create_button = QPushButton("", self.content_widget)
        self.tag_create_button.clicked.connect(
            lambda: self.tag_create_requested.emit(""))

        self.layer_tree_popup = LayerTreePopup(self.content_widget)
        tree_view = self.layer_tree_popup.tree_view
        tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)

        # ---- 折叠切换 ----
        self.collapse_toggle_button = QPushButton("", self.content_widget)
        self.collapse_toggle_button.clicked.connect(self.collapse_toggle)

        self.content_layout.addWidget(self.tags_view, 1)
        self.content_layout.addWidget(self.tag_add_container_widget)
        self.content_layout.addWidget(self.tag_create_button)
        self.content_layout.addWidget(self.layer_tree_button)

        self.main_layout.addWidget(self.content_widget, 1)
        self.main_layout.addWidget(self.collapse_toggle_button)
        self.main_layout.addStretch()

        # ---- 内部连线 ----
        self.tags_view.chip_activated.connect(self.chip_activated)
        self.tags_view.chip_remove_requested.connect(self.chip_remove_requested)
        self.tag_apply_button.clicked.connect(self._emit_apply)
        self.tag_line_edit.returnPressed.connect(self._emit_apply)
        self.layer_tree_popup.tree_view.clicked.connect(self._on_tree_clicked)
        self.layer_tree_popup.tree_view.activated.connect(self._on_tree_clicked)
        self.layer_tree_popup.tree_view.installEventFilter(self)

        self.setFixedHeight(64)

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用输入提示 / 按钮 / tooltip 文案。"""
        self.tag_line_edit.setPlaceholderText(self.tr("标签名，可用 / 分层"))
        self.tag_apply_button.setText(self.tr("应用"))
        self.tag_apply_button.setToolTip(self.tr("添加到当前图片"))
        self.layer_tree_button.setText(self.tr("标签树"))
        self.tag_create_button.setText(self.tr("新建"))
        self.tag_create_button.setToolTip(self.tr("新建标签"))
        self._apply_toggle_symbol()

    def _apply_toggle_symbol(self) -> None:
        """把当前折叠符号应用到切换按钮（图标 / 文本都支持）。"""
        symbol = self.expand_icon if self._is_collapse else self.collapse_icon
        button = self.collapse_toggle_button
        if isinstance(symbol, (QIcon, QPixmap)):
            button.setIcon(symbol)
            button.setText("")
        else:
            button.setIcon(QIcon())
            button.setText(str(symbol))

    # ------------------------------------------------------------------ #
    # 标签树弹窗
    # ------------------------------------------------------------------ #
    def _on_toggle_layer_tree_popup(self) -> None:
        popup = self.layer_tree_popup
        if popup.isVisible():
            popup.hide()
            self._popup_closed_at = time.monotonic()
            return
        self.layer_tree_open_requested.emit()  # 先让外部刷新树数据
        # 100ms：只挡「同一次鼠标点击 / 连按造成的开-关抖动」，不挡正常的再次打开
        if time.monotonic() - self._popup_closed_at < 0.1:
            return
        popup.show_under(self.layer_tree_button)
        # 打开后把焦点交给标签树，方向键 + Enter 即可操作
        tree = popup.tree_view
        tree.setFocus(Qt.FocusReason.PopupFocusReason)
        if not tree.currentIndex().isValid() and tree.model() is not None:
            first = tree.model().index(0, 0)
            if first.isValid():
                tree.setCurrentIndex(first)

    def _emit_apply(self) -> None:
        text = self.tag_line_edit.text().strip()
        if text:
            self.apply_requested.emit(text)

    def _on_tree_clicked(self, index) -> None:
        tag_id = _tag_id(index.data(TREE_TAG_ROLE))
        if tag_id:
            self.layer_tree_activated.emit(tag_id)
            self.layer_tree_popup.hide()

    def _tree_current_tag_id(self) -> str:
        """标签树当前节点的标识（没有选中时为空串）。"""
        tree = self.layer_tree_popup.tree_view
        index = tree.currentIndex()
        if not index.isValid():
            return ""
        return _tag_id(index.data(TREE_TAG_ROLE))

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        """标签树的键盘快捷键（树弹窗是独立键盘抓取窗口，走本地事件）。"""
        tree = self.layer_tree_popup.tree_view
        if obj is tree and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            tag_id = self._tree_current_tag_id()

            if key == Qt.Key.Key_F2 and tag_id:
                self.tag_rename_requested.emit(tag_id)
                event.accept()
                return True
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and tag_id:
                self.tag_delete_requested.emit(tag_id)
                event.accept()
                return True
            if key == Qt.Key.Key_Insert:
                # Insert = 在当前节点下新建；无选中 → 新建根标签
                self.tag_create_requested.emit(tag_id)
                event.accept()
                return True
            if (key == Qt.Key.Key_N
                    and mods & Qt.KeyboardModifier.ControlModifier):
                # Ctrl+N = 总是新建根标签
                self.tag_create_requested.emit("")
                event.accept()
                return True
            if key == Qt.Key.Key_Menu or (
                    key == Qt.Key.Key_F10
                    and mods & Qt.KeyboardModifier.ShiftModifier):
                index = tree.currentIndex()
                rect = tree.visualRect(index) if index.isValid() else None
                pos = (rect.center()
                       if rect is not None and not rect.isEmpty()
                       else tree.viewport().rect().center())
                self._on_tree_context_menu(pos)
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def _on_tree_context_menu(self, pos) -> None:
        """标签树右键菜单：新建 / 重命名 / 删除（只声明意图）。"""
        tree = self.layer_tree_popup.tree_view
        index = tree.indexAt(pos)
        tag_id = _tag_id(index.data(TREE_TAG_ROLE)) if index.isValid() else ""

        menu = QMenu(tree)
        create_action = menu.addAction(
            self.tr("新建子标签") if tag_id else self.tr("新建根标签"))
        create_action.triggered.connect(
            lambda: self.tag_create_requested.emit(tag_id))

        if tag_id:
            menu.addSeparator()
            rename_action = menu.addAction(self.tr("重命名标签"))
            rename_action.triggered.connect(
                lambda: self.tag_rename_requested.emit(tag_id))
            delete_action = menu.addAction(self.tr("删除标签"))
            delete_action.triggered.connect(
                lambda: self.tag_delete_requested.emit(tag_id))

        menu.exec(tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    #: 配置键 → 处理函数；函数按 ``(self, *参数)`` 调用。
    _CONFIG_MAP: dict[str, Callable[..., object]] = {
        "layer_tree_icon":
            lambda self, icon: self.layer_tree_button.setIcon(icon),
        "collapse_icon": lambda self, icon: self._set_toggle_symbol(
            "collapse_icon", icon),
        "expand_icon": lambda self, icon: self._set_toggle_symbol(
            "expand_icon", icon),
    }

    def _set_toggle_symbol(self, name: str, symbol: Any) -> None:
        """记录折叠符号并刷新切换按钮。"""
        setattr(self, name, symbol)
        self._apply_toggle_symbol()

    def config(self, config: Mapping[str, Any]) -> list[str]:
        """按外部配置刷新外观；返回未识别的键。

        值可以写标量（``{"collapse_icon": icon}``），也可以写参数序列
        （``{"collapse_icon": [icon]}``），两种写法等价。
        """
        unknown: list[str] = []
        for key, value in config.items():
            handler = self._CONFIG_MAP.get(key)
            if handler is None:
                unknown.append(key)
                continue
            handler(self, *self._config_args(value))
        return unknown

    @staticmethod
    def _config_args(value: Any) -> tuple[Any, ...]:
        """配置值 → 参数元组：序列原样展开，标量包一层。"""
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(value)
        return (value,)

    # 兼容旧名字
    update_config = config

    # ------------------------------------------------------------------ #
    # 折叠
    # ------------------------------------------------------------------ #
    def expand(self) -> None:
        """展开标签栏（恢复输入区宽度）。"""
        self._is_collapse = False
        # 先恢复可见性再恢复宽度：隐藏时定宽不会生效，顺序反了会短暂闪一下。
        self.content_widget.setVisible(True)
        if self._old_tag_add_container_widget is not None:  # 防 2**24-1
            self.content_widget.setFixedWidth(
                self._old_tag_add_container_widget)
        self._apply_toggle_symbol()

    def collapse(self) -> None:
        """收起标签栏（输入区宽度归零）。"""
        if self._is_collapse:
            return  # 已在折叠状态，不重复记忆宽度
        self._is_collapse = True
        self._old_tag_add_container_widget = self.content_widget.width()
        # 「宽度归零」不等于「不可见」：Qt 的 Tab 链只看 isVisible()，0 宽控件
        # 照样停在链上，焦点会卡在看不见的输入框里。先把焦点交出去，再真正隐藏。
        self._release_child_focus()
        self.content_widget.setFixedWidth(0)
        self.content_widget.setVisible(False)
        self._apply_toggle_symbol()

    def _release_child_focus(self) -> None:
        """把焦点从 ``content_widget`` 子树里交出去（交给折叠切换按钮）。"""
        app = QApplication.instance()
        focused = app.focusWidget() if isinstance(app, QApplication) else None
        if focused is not None and self.content_widget.isAncestorOf(focused):
            self.collapse_toggle_button.setFocus(
                Qt.FocusReason.OtherFocusReason)

    def collapse_toggle(self, to: bool | None = None) -> None:
        """切换折叠状态；``to`` 为 ``None`` 时取反。"""
        if to is None:
            self.collapse_toggle(not self._is_collapse)
            return
        if to:
            self.collapse()
        else:
            self.expand()

    # ------------------------------------------------------------------ #
    # 数据注入（view 接口：外部把 model 塞进来）
    # ------------------------------------------------------------------ #
    def set_chip_model(self, model) -> None:
        """设置 chip 列表模型。

        :param model: 任意实现 ``DisplayRole`` + ``TAG_ROLE`` 的模型，
            通常是 :class:`~albuswall.ui.model.TagChipModel`。
        """
        self.tags_view.setModel(model)

    def set_tree_model(self, model) -> None:
        """设置标签树模型。

        :param model: 任意实现 ``TREE_TAG_ROLE`` 的树模型，通常是
            :class:`~albuswall.ui.model.TagTreeModel`。
        """
        self.layer_tree_popup.tree_view.setModel(model)
        self.layer_tree_popup.tree_view.expandAll()

    def bind_models(self, chip_model=None, tree_model=None) -> None:
        """一次装好两个模型（任一项为 ``None`` 时跳过）。"""
        if chip_model is not None:
            self.set_chip_model(chip_model)
        if tree_model is not None:
            self.set_tree_model(tree_model)

    def tags_model(self):
        """当前 chip 列表模型（未注入时为 ``None``）。"""
        return self.tags_view.model()

    def tree_model(self):
        """当前标签树模型（未注入时为 ``None``）。"""
        return self.layer_tree_popup.tree_view.model()

    def clear_apply_input(self) -> None:
        """清空输入框。"""
        self.tag_line_edit.clear()
