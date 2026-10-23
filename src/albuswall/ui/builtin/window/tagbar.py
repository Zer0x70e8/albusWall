#
""""""

import time
from typing import Any, Sequence, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QListView, QLineEdit,
    QWidget, QTreeView, QVBoxLayout, QSizePolicy, QFrame, QMenu
)

from ..i18n import Retranslatable
from ..widgets.anchored_popup import AnchoredPopup
from ..widgets import BlurLabel
from ..widgets.tag_chip_view import TagChipView


class LayerTreePopup(AnchoredPopup):
    main_layout: QVBoxLayout

    tree_view: QTreeView

    def __init__(self, parent=None):
        super().__init__(parent)

        self._setup_ui()

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        self.tree_view = QTreeView(self)

        self.tree_view.setHeaderHidden(True)
        self.tree_view.setUniformRowHeights(True)

        self.main_layout.addWidget(self.tree_view)


class TagBar(Retranslatable, QFrame):
    # signal
    apply_requested = Signal(str)  # 输入框文本
    chip_activated = Signal(str)  # uuid
    chip_remove_requested = Signal(str)  # uuid
    layer_tree_open_requested = Signal()  # 弹出/关闭切换前
    layer_tree_activated = Signal(str)  # uuid
    tag_create_requested = Signal(str)  # 父标签 uuid；空串 = 根
    tag_rename_requested = Signal(str)  # uuid
    tag_delete_requested = Signal(str)  # uuid

    #
    main_layout: QHBoxLayout
    content_widget: BlurLabel
    content_layout: QHBoxLayout

    tags_view: TagChipView

    tag_add_container_widget: QWidget
    tag_add_container_layout: QHBoxLayout
    tag_line_edit: QLineEdit
    tag_apply_button: QPushButton

    layer_tree_button: QPushButton

    collapse_toggle_button: QPushButton

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._is_collapse = False
        self._old_tag_add_container_widget: int | None = None
        self._popup_closed_at: float = 0.0
        self.expand_icon = ">"
        self.collapse_icon = "<"

        self._setup_ui()
        self.retranslate_ui()

    def _setup_ui(self):
        self.content_widget = BlurLabel(self, draw_label_content=False)
        layout = QHBoxLayout(self.content_widget)
        self.content_layout = layout
        self.main_layout = QHBoxLayout(self)

        self.content_widget.target_widget = self

        # tags
        self.tags_view = TagChipView(self.content_widget)

        self.tags_view.setFlow(QListView.Flow.LeftToRight)
        self.tags_view.setWrapping(False)
        self.tags_view.setUniformItemSizes(False)  # chip 宽度不固定
        self.tags_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tags_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tags_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred
        )

        self.tag_add_container_widget = QWidget(self.content_widget)
        self.tag_add_container_layout = QHBoxLayout(
            self.tag_add_container_widget)

        self.tag_add_container_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_add_container_layout.setSpacing(0)

        self.tag_line_edit = QLineEdit(
            self.tag_add_container_widget)
        self.tag_apply_button = QPushButton(
            self.tag_add_container_widget)

        self.tag_add_container_layout.addWidget(
            self.tag_line_edit)
        self.tag_add_container_layout.addWidget(
            self.tag_apply_button)

        # layer tree
        self.layer_tree_button = QPushButton("", self.content_widget)
        self.layer_tree_button.clicked.connect(
            self._on_toggle_layer_tree_popup)

        self.tag_create_button = QPushButton("", self.content_widget)
        self.tag_create_button.clicked.connect(
            lambda: self.tag_create_requested.emit(""))

        self.layer_tree_popup = LayerTreePopup(
            self.content_widget)

        tree_view = self.layer_tree_popup.tree_view
        tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree_view.customContextMenuRequested.connect(
            self._on_tree_context_menu)

        # collapse toggle
        self.collapse_toggle_button = QPushButton(
            self.collapse_icon, self.content_widget
        )

        layout.addWidget(self.tags_view, 1)
        layout.addWidget(self.tag_add_container_widget)
        layout.addWidget(self.tag_create_button)
        layout.addWidget(self.layer_tree_button)

        self.main_layout.addWidget(self.content_widget, 1)
        self.main_layout.addWidget(self.collapse_toggle_button)
        self.main_layout.addStretch()

        # wire
        self.collapse_toggle_button.clicked.connect(
            lambda: self.collapse_toggle()
        )

        # 透出 chip 信号
        self.tags_view.chip_activated.connect(self.chip_activated)
        self.tags_view.chip_remove_requested.connect(self.chip_remove_requested)

        # ... 剩余代码保持原样 ...

        # apply 透出（把两个来源合并成一个信号）
        self.tag_apply_button.clicked.connect(self._emit_apply)
        self.tag_line_edit.returnPressed.connect(self._emit_apply)

        # 树点击透出
        self.layer_tree_popup.tree_view.clicked.connect(self._on_tree_clicked)

        self.setFixedHeight(64)

    def retranslate_ui(self) -> None:
        """重新应用标签栏的输入提示 / 按钮 / tooltip 文案。"""
        self.tag_line_edit.setPlaceholderText(self.tr("标签名，可用 / 分层"))
        self.tag_apply_button.setText(self.tr("apply"))
        self.tag_apply_button.setToolTip(self.tr("添加到当前图片"))
        self.layer_tree_button.setText(self.tr("layers"))
        self.tag_create_button.setText(self.tr("new"))
        self.tag_create_button.setToolTip(self.tr("新建标签"))
        self.collapse_toggle_button.setText(
            self.expand_icon if self._is_collapse else self.collapse_icon
        )

    def _on_toggle_layer_tree_popup(self) -> None:
        popup = self.layer_tree_popup
        if popup.isVisible():
            popup.hide()
            self._popup_closed_at = time.monotonic()
            return
        self.layer_tree_open_requested.emit()  # 先让 presenter 刷新
        if time.monotonic() - getattr(self, "_popup_closed_at", 0.0) < 0.2:
            return
        popup.show_under(self.layer_tree_button)

    def _emit_apply(self) -> None:
        text = self.tag_line_edit.text().strip()
        if text:
            self.apply_requested.emit(text)

    def _on_tree_clicked(self, index) -> None:
        uuid = index.data(Qt.ItemDataRole.UserRole + 1)
        if uuid:
            self.layer_tree_activated.emit(str(uuid.uuid))
            self.layer_tree_popup.hide()

    def _on_tree_context_menu(self, pos) -> None:
        """标签树右键菜单：新建子标签 / 重命名 / 删除。

        菜单只声明用户意图，具体的权限 / 冲突 / 级联询问都交给
        TagEditorPresenter 和 TagService。
        """
        tree = self.layer_tree_popup.tree_view
        index = tree.indexAt(pos)
        tag = index.data(Qt.ItemDataRole.UserRole + 1) if index.isValid() else None
        tag_uuid = str(tag.uuid) if tag else ""

        menu = QMenu(tree)
        create_action = menu.addAction(
            self.tr("新建子标签") if tag else self.tr("新建根标签")
        )
        create_action.triggered.connect(
            lambda: self.tag_create_requested.emit(tag_uuid)
        )

        if tag:
            menu.addSeparator()
            rename_action = menu.addAction(self.tr("重命名标签"))
            rename_action.triggered.connect(
                lambda: self.tag_rename_requested.emit(tag_uuid)
            )
            delete_action = menu.addAction(self.tr("删除标签"))
            delete_action.triggered.connect(
                lambda: self.tag_delete_requested.emit(tag_uuid)
            )

        menu.exec(tree.viewport().mapToGlobal(pos))

    # interface
    # noinspection none-function-assignment
    _CONFIG_MAP: dict[str, Callable] = {
        "layer_tree_icon":
            lambda self, icon:
            self.layer_tree_button.setIcon(icon),
        "collapse_icon":
            lambda self, icon: (
                setattr(self, "collapse_icon", icon),
                self.collapse_toggle_button.setIcon(icon)
            ),
        "expand_icon":
            lambda self, icon: (
                setattr(self, "expand_icon", icon),
                self.collapse_toggle_button.setIcon(icon)
            ),
    }

    def update_config(self, data: dict[str, Sequence[Any]]) -> list:
        unknows = []
        for k, v in data.items():
            if k in self._CONFIG_MAP:
                self._CONFIG_MAP[k](self, *v)
            else:
                unknows.append(k)
        return unknows

    def expand(self) -> None:
        self._is_collapse = False
        if self._old_tag_add_container_widget is not None:  # 判 None，防 2**24-1
            self.content_widget.setFixedWidth(self._old_tag_add_container_widget)
        self.collapse_toggle_button.setText(self.collapse_icon)

    def collapse(self) -> None:
        self._is_collapse = True
        self._old_tag_add_container_widget = self.content_widget.width()
        self.content_widget.setFixedWidth(0)
        self.collapse_toggle_button.setText(self.expand_icon)

    def collapse_toggle(self, to: Optional[bool] = None):
        if to is None:
            self.collapse_toggle(not self._is_collapse)
            return
        if to:
            self.collapse()
        else:
            self.expand()

    # ---- 供 presenter 直接设 model ----
    def set_chip_model(self, model) -> None:
        self.tags_view.setModel(model)

    def set_tree_model(self, model) -> None:
        self.layer_tree_popup.tree_view.setModel(model)

    # ---- 打开前通知 presenter 刷新树 ----
    def clear_apply_input(self) -> None:
        self.tag_line_edit.clear()

    # alias
    config = update_config
