#
"""设置页的**被动视图**（``SettingView``）。

输入只有两样：**一份设置树**（``model.SettingTree``，不可变）、**变更通知**。
输出只有两样：**全量可编辑 binder 字典**、**提交结果按 key 回填**。视图不读
配置器、不写文件、不推断类型、不求值 i18n、不调度线程（线程契约见下）。

对外契约
========

::

    view.set_dump_schema(dump)            # 给我新 dump（schema_version 不认识就抛）
    view.set_tree(tree)                   # 已经 normalize_dump 过时直接装
    view.on_node_changed(node, value, src) # 可能跨线程：set/remove/reset
    view.on_reload()                       # 整文件重载：全量重建

    view.collect_values()                  # {key: typed_value}，全量可编辑 binder
    view.show_submit_result(result)        # validation_errors 按 key 回填；pending/ok 进 footer
    view.submit_requested(mode, values)     # 三个按钮的意图信号（由外部提交）

重建策略
========

**装树才重建**（``_build_content``）；范围 / 高级开关只切可见性
（``_apply_visibility``），不销毁控件、不动 binder。于是切范围是毫秒级，
滚动位置与焦点不会被范围切换清掉。

视图私有状态（外部只通过上面几个入口影响它）
==========================================

``current_scope`` / ``current_anchor`` / ``show_advanced`` / ``binders`` /
``anchor_map``（= 全部锚点 ``_all_anchors`` 的可见子集）。

线程契约
========

``on_node_changed`` / ``on_reload`` **可能在工作线程被调用**。视图内部用
Qt 信号做边界：发射时若与视图不在同一线程，Qt 会排队到视图所属线程执行；
主线程调用则同步执行。「谁给我回调谁负责切主线程」也成立——两种都行，但
契约上写明：**回调可能不在主线程**。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import strings
from .binder import LeafBinder
from .content import (
    AUTO_COLLAPSE_ITEM_COUNT,
    UNBOUND_GROUP_KEY,
    UNCOVERED_GROUP_KEY,
    group_visible,
    item_visible,
    ordered_children,
    scope_from_text,
    section_visible,
    unbound_group,
    uncovered_group,
    visible_count,
)
from .contract import (
    Scope,
    ScopeKind,
    SubmitMode,
    normalize_dump,
)
from .group_box import SettingGroupBox
from .leaf_row import SettingRow
from .model import SettingTree
from .nav import SettingsNav
from .preview import SettingsPreviewDialog

__all__ = ["SettingView", "SettingsPreviewDialog"]

logger = logging.getLogger(__package__)



class SettingView(QWidget):
    """配置设置页的被动视图（纯视图：只吃 dump / 通知，只吐值 / 渲染结果）。"""

    #: 提交意图：``(mode, {key: typed_value})``。视图自己不调用写回层。
    submit_requested = Signal(str, object)
    #: footer「重试」按钮（``pending`` 非空时可见）。
    retry_requested = Signal()
    #: 外部重载意图（视图不自己 dump；由 presenter 决定要不要重新提供数据）。
    reload_requested = Signal()
    #: 范围 / 高级开关 / 锚点变化（只在点击左侧锚点时触发）。
    scope_changed = Signal(object)
    show_advanced_changed = Signal(bool)
    anchor_changed = Signal(object)
    #: 「再处理」意图：移动条目 / 恢复默认位置 / 添加组件分组。
    #: 视图只发意图，持久化与 schema 登记全部在 presenter。
    move_requested = Signal(str)
    reset_move_requested = Signal(str)
    add_component_requested = Signal()

    #: 跨线程通知的边界（见模块 docstring）。
    _node_changed_ready = Signal(object, object, str)
    _reload_ready = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingSetting")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._tree: SettingTree | None = None
        self._binders: dict[str, LeafBinder] = {}
        self._node_to_keys: dict[tuple[str, str], list[str]] = {}
        self._rows: dict[str, SettingRow] = {}
        self._group_boxes: dict[str, SettingGroupBox] = {}
        self._group_parent: dict[str, str | None] = {}
        self._item_parent: dict[str, str] = {}
        self._empty_hint: QLabel | None = None
        #: 左侧导航（锚点按钮 + 两步焦点语义）；在 ``_build_ui`` 里创建。
        self._nav: SettingsNav | None = None

        self._current_scope: Scope = Scope.all()
        self._show_advanced = True
        self._pending_keys: set[str] = set()
        self._preview_dialog: SettingsPreviewDialog | None = None
        self._node_changed_ready.connect(self._apply_node_changed)
        self._reload_ready.connect(self._rebuild_from_current_tree)

        self._build_ui()
        self._update_header()
        self._update_dirty_label()

    # ==================================================================
    # 对外属性（只读；外部不要直接改）
    # ==================================================================

    @property
    def dump(self) -> SettingTree | None:
        """当前设置树（不可变；旧式 ``dump["groups"]`` 访问仍然可用）。"""
        return self._tree

    @property
    def tree(self) -> SettingTree | None:
        return self._tree

    @property
    def binders(self) -> Mapping[str, LeafBinder]:
        return MappingProxyType(self._binders)

    @property
    def rows(self) -> Mapping[str, SettingRow]:
        """当前**可见**的叶子行（``key → SettingRow``；随范围 / 高级开关变化）。

        行的控件本身在整棵树安装时一次建好，范围 / 高级开关只切可见性，
        所以这里按 ``isVisibleTo`` 过滤，而不是"重建后剩下的那些"。
        """
        return MappingProxyType(
            {key: row for key, row in self._rows.items() if row.isVisibleTo(self)},
        )

    @property
    def anchor_map(self) -> Mapping[str, QWidget]:
        nav = self._nav
        return MappingProxyType(nav.visible_anchors if nav is not None else {})

    @property
    def _sidebar_buttons(self) -> Mapping[str, QPushButton]:
        """兼容旧访问：左侧锚点按钮表（只读）。"""
        nav = self._nav
        return nav.buttons if nav is not None else {}

    @property
    def current_scope(self) -> Scope:
        return self._current_scope

    @property
    def current_anchor(self) -> str | None:
        nav = self._nav
        return nav.current if nav is not None else None

    @property
    def show_advanced(self) -> bool:
        return self._show_advanced

    def row_for(self, key: str) -> SettingRow | None:
        """可见行（隐藏的行当作不存在，与"重建后消失"旧语义等价）。"""
        row = self._rows.get(key)
        return row if row is not None and row.isVisibleTo(self) else None

    def focus_first(self) -> bool:
        """把键盘焦点交给第一个可见的可编辑控件（进入设置页时用）。

        依次尝试：叶子编辑控件 → 左侧锚点按钮 → 范围下拉框。返回是否成功；
        调用方（页面）在失败时可以落到关闭按钮上。
        """
        for row in self.rows.values():
            control = row.control
            if not control.isEnabled():
                continue
            if control.focusPolicy() == Qt.FocusPolicy.NoFocus:
                continue
            if not control.isVisibleTo(self):
                continue
            control.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        for button in self._sidebar_buttons.values():
            if button.isVisibleTo(self):
                button.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        if self._scope_combo.isVisibleTo(self):
            self._scope_combo.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return False

    @property
    def preview_dialog(self) -> SettingsPreviewDialog | None:
        """``dry_run`` 预览窗口（没有打开时为 ``None``）。"""
        return self._preview_dialog

    @property
    def jump_frame_target(self) -> str | None:
        """当前挂了「跳转焦点框」的锚点 key（没有显示时为 ``None``）。"""
        nav = self._nav
        return nav.jump_target if nav is not None else None

    @property
    def jump_frame_visible(self) -> bool:
        """虚线焦点框当前是否可见（第二次跳转给焦点后会隐藏）。"""
        nav = self._nav
        return nav.jump_visible if nav is not None else False

    @property
    def jump_focus_given(self) -> bool:
        """最近一次跳转是否已经把键盘焦点交给目标组件。"""
        nav = self._nav
        return nav.jump_focus_given if nav is not None else False

    # ==================================================================
    # 构造 UI
    # ==================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        # ---- header --------------------------------------------------
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 4, 0)
        header.setSpacing(10)
        self._title = QLabel(strings.HEADER_TITLE, self)
        self._title.setObjectName("SettingHeaderTitle")
        self._summary = QLabel("", self)
        self._summary.setObjectName("SettingHeaderSummary")
        self._status_label = QLabel(strings.READY, self)
        self._status_label.setObjectName("SettingHeaderStatus")
        self._advanced_toggle = QCheckBox(strings.ADVANCED_TOGGLE, self)
        self._advanced_toggle.setObjectName("SettingAdvancedToggle")
        self._advanced_toggle.setChecked(True)
        self._advanced_toggle.toggled.connect(self.set_show_advanced)
        self._reload_button = QPushButton("⟳", self)
        self._reload_button.setObjectName("SettingReloadButton")
        self._reload_button.setToolTip(strings.RELOAD_TOOLTIP)
        self._reload_button.clicked.connect(self.reload_requested.emit)
        self._add_component_button = QPushButton(strings.ADD_COMPONENT, self)
        self._add_component_button.setObjectName("SettingAddComponentButton")
        self._add_component_button.setToolTip(strings.ADD_COMPONENT_TOOLTIP)
        self._add_component_button.clicked.connect(
            self.add_component_requested.emit,
        )

        header.addWidget(self._title, 0)
        header.addWidget(self._summary, 0)
        header.addStretch(1)
        header.addWidget(self._status_label, 0)
        header.addWidget(self._advanced_toggle, 0)
        header.addWidget(self._add_component_button, 0)
        header.addWidget(self._reload_button, 0)
        root.addLayout(header)

        # ---- body: 左侧导航 / 右侧内容 --------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setObjectName("SettingSplitter")
        splitter.setChildrenCollapsible(False)

        left = QWidget(splitter)
        left.setObjectName("SettingNav")
        left.setMinimumWidth(190)
        left.setMaximumWidth(300)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(2, 4, 2, 4)
        left_layout.setSpacing(6)

        self._scope_combo = QComboBox(left)
        self._scope_combo.setObjectName("SettingScopeCombo")
        self._scope_combo.setToolTip(strings.SCOPE_TOOLTIP)
        self._scope_combo.currentIndexChanged.connect(
            self._on_scope_combo_changed,
        )
        left_layout.addWidget(self._scope_combo, 0)

        sidebar_scroll = QScrollArea(left)
        sidebar_scroll.setObjectName("SettingNavScroll")
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._sidebar_content = QWidget(sidebar_scroll)
        self._sidebar_content.setObjectName("SettingNavContent")
        self._sidebar_layout = QVBoxLayout(self._sidebar_content)
        self._sidebar_layout.setContentsMargins(0, 0, 0, 0)
        self._sidebar_layout.setSpacing(0)
        self._sidebar_layout.addStretch(1)
        sidebar_scroll.setWidget(self._sidebar_content)
        left_layout.addWidget(sidebar_scroll, 1)

        right = QScrollArea(splitter)
        right.setObjectName("SettingScrollArea")
        right.setWidgetResizable(True)
        right.setFrameShape(QFrame.Shape.NoFrame)
        self._right_scroll = right
        self._right_content = QWidget(right)
        self._right_content.setObjectName("SettingContent")
        self._right_layout = QVBoxLayout(self._right_content)
        self._right_layout.setContentsMargins(10, 4, 10, 12)
        self._right_layout.setSpacing(4)
        right.setWidget(self._right_content)
        # 左侧导航（锚点按钮 + 两步焦点语义 + 虚线焦点框）。
        self._nav = SettingsNav(
            self,
            sidebar_parent=self._sidebar_content,
            sidebar_layout=self._sidebar_layout,
            right_scroll=right,
            right_content=self._right_content,
            host=self,
        )

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 820])
        root.addWidget(splitter, 1)

        # ---- footer --------------------------------------------------
        footer = QFrame(self)
        footer.setObjectName("SettingFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(4, 0, 4, 0)
        footer_layout.setSpacing(8)
        self._footer_status = QLabel("", footer)
        self._footer_status.setObjectName("SettingFooterStatus")
        self._footer_status.setWordWrap(True)
        self._dirty_label = QLabel("", footer)
        self._dirty_label.setObjectName("SettingFooterHint")
        self._retry_button = QPushButton(strings.BUTTON_RETRY, footer)
        self._retry_button.setObjectName("SettingRetryButton")
        self._retry_button.setVisible(False)
        self._retry_button.clicked.connect(self.retry_requested.emit)

        self._preview_button = QPushButton(SubmitMode.DRY_RUN.text, footer)
        self._preview_button.setObjectName("SettingPreviewButton")
        self._preview_button.clicked.connect(
            lambda: self._emit_submit(SubmitMode.DRY_RUN),
        )
        self._save_button = QPushButton(SubmitMode.COLLECT_ERRORS.text, footer)
        self._save_button.setObjectName("SettingSaveButton")
        self._save_button.clicked.connect(
            lambda: self._emit_submit(SubmitMode.COLLECT_ERRORS),
        )
        self._draft_button = QPushButton(SubmitMode.DRAFT.text, footer)
        self._draft_button.setObjectName("SettingDraftButton")
        self._draft_button.clicked.connect(
            lambda: self._emit_submit(SubmitMode.DRAFT),
        )

        footer_layout.addWidget(self._footer_status, 1)
        footer_layout.addWidget(self._dirty_label, 0)
        footer_layout.addWidget(self._retry_button, 0)
        footer_layout.addWidget(self._preview_button, 0)
        footer_layout.addWidget(self._draft_button, 0)
        footer_layout.addWidget(self._save_button, 0)
        root.addWidget(footer, 0)

    # ==================================================================
    # 输入：dump
    # ==================================================================

    def set_dump_schema(self, dump: Mapping[str, Any]) -> SettingTree:
        """接收一份新 dump，校验版本后全量重建。

        这是「给我新 dump」的唯一入口；``on_node_changed`` 只改单个 binder，
        不走这里。版本不认识时抛
        :class:`~albuswall.ui.setting.model.UnsupportedSchemaVersionError`，
        现有页面保持原样（不会渲染半份数据）。

        Returns:
            归一化后的不可变设置树（调用方可只读查看）。
        """
        return self.set_tree(normalize_dump(dump))

    def set_tree(self, tree: SettingTree) -> SettingTree:
        """直接安装一棵已归一化的设置树（presenter 已经 parse 过时用）。"""
        self._install_tree(tree)
        return tree

    def _install_tree(self, tree: SettingTree) -> None:
        self._tree = tree
        self._binders = self._create_binders(tree)
        self._node_to_keys = {}
        for key, binder in self._binders.items():
            self._node_to_keys.setdefault(binder.node, []).append(key)
        self._current_scope = self._validate_scope(self._current_scope)
        self._pending_keys.clear()
        self._retry_button.setVisible(False)
        self._footer_status.setText("")
        self._set_status(strings.READY)
        self._sync_scope_combo()
        self._build_content()
        self._apply_visibility()
        self._update_header()
        self._update_dirty_label()

    def _create_binders(self, tree: SettingTree) -> dict[str, LeafBinder]:
        binders: dict[str, LeafBinder] = {}
        for leaf in tree.items:
            binder = LeafBinder(leaf)
            binder.add_listener(self._on_binder_event)
            binders[binder.key] = binder
        return binders

    def _validate_scope(self, scope: Scope) -> Scope:
        """范围失效时回退「全部」（文件不再存在 / 有部分已消失）。"""
        if self._tree is None:
            return Scope.all()
        if scope.kind is ScopeKind.FILE and scope.file not in self._tree.by_file:
            return Scope.all()
        return scope

    # ==================================================================
    # 输入：通知（可能跨线程）
    # ==================================================================

    def on_node_changed(
            self, node: tuple[str, str] | list[Any], value: Any, source: str,
    ) -> None:
        """外部值变更通知：``(section, key)`` + 新值 + 来源。

        ``source`` 取 ``set`` / ``remove`` / ``reset``；``reload`` 会被当成
        整页重载（与 :meth:`on_reload` 等价）。

        **可能在工作线程调用**：方法只做参数收口，然后发射 Qt 信号；跨线程
        时由 Qt 排队到视图线程执行。非法参数立即抛错（不会静默吞掉）。
        """
        if source == "reload":
            self.on_reload()
            return
        if source not in ("set", "remove", "reset"):
            raise ValueError(
                f"unknown notification source {source!r}; "
                f"expected set/remove/reset/reload",
            )
        if not isinstance(node, (tuple, list)) or len(node) != 2:
            raise TypeError(
                f"node must be (section, key), got {node!r}",
            )
        section, key = str(node[0]), str(node[1])
        self._node_changed_ready.emit(
            (section, key), deepcopy(value), str(source),
        )

    def on_reload(self) -> None:
        """整文件重载：全量重建（字段少，重建比 diff 便宜且稳）。

        **可能在工作线程调用**。视图不会自己去 dump；整页会按**当前持有**
        的 dump 重建（用户未提交的改动会被丢弃）。数据本身也变了的话，
        请 presenter 先调用 :meth:`set_dump_schema`。
        """
        self._reload_ready.emit()

    def _apply_node_changed(
            self, node: Any, value: Any, source: str,
    ) -> None:
        keys = self._node_to_keys.get(tuple(node), ())
        if not keys:
            logger.debug("setting view: no binder for node %r", node)
            return
        changed = False
        for key in keys:
            binder = self._binders.get(key)
            if binder is None:
                continue
            changed = binder.set_external(value, source) or changed
        if not changed:
            return
        self._set_status(strings.STATUS_SYNCED, level="info")
        self._update_dirty_label()

    def _rebuild_from_current_tree(self) -> None:
        if self._tree is None:
            return
        self._install_tree(self._tree)
        self._set_status(strings.STATUS_RELOADED, level="info")

    # ==================================================================
    # 视图状态
    # ==================================================================

    def set_scope(self, scope: Scope | str) -> None:
        """切换范围：重建右侧、清空并重建 ``anchor_map``、滚动回顶部。

        失效范围（文件不存在）回退「全部」；选中锚点不在了就清空高亮。
        """
        if isinstance(scope, str):
            scope = scope_from_text(scope)
        scope = self._validate_scope(scope)
        if scope == self._current_scope and self._tree is not None:
            return
        self._current_scope = scope
        self._sync_scope_combo()
        self._apply_visibility()
        self._right_scroll.verticalScrollBar().setValue(0)
        self.scope_changed.emit(scope)

    def set_show_advanced(self, enabled: bool) -> None:
        """高级折叠区开关：只影响布局，不影响 binder / 提交内容。"""
        enabled = bool(enabled)
        if enabled == self._show_advanced:
            return
        self._show_advanced = enabled
        with QSignalBlocker(self._advanced_toggle):
            self._advanced_toggle.setChecked(enabled)
        self._apply_visibility()
        self.show_advanced_changed.emit(enabled)

    def select_anchor(self, key: str) -> bool:
        """左侧点锚点：委托给 :class:`~albuswall.ui.setting.nav.SettingsNav`。

        两步语义（详见 nav 模块 docstring）：

        * 第一次：只画虚线焦点框，**不给键盘焦点**；
        * 再点同一个锚点：焦点框已显示 → 把键盘焦点交给目标组件并隐藏虚线框；
        * 点别的锚点：重新从第一步开始；
        * 目标不可聚焦（只读 / 未绑定 / 未声明）时保持虚线框。
        """
        nav = self._nav
        return nav is not None and nav.select(key)

    # ---- NavHost 协议：导航需要视图提供的三件事 -------------------------

    def expand_chain(self, key: str) -> None:
        """展开某个锚点的父分组链（让目标出现在可视区里）。"""
        group_key = key if key in self._group_boxes else self._item_parent.get(key)
        if key in self._group_boxes:
            # 点分组锚点也顺便展开自己
            self._group_boxes[key].setChecked(True)
            group_key = self._group_parent.get(key)
        while group_key is not None:
            box = self._group_boxes.get(group_key)
            if box is not None:
                box.setChecked(True)
            group_key = self._group_parent.get(group_key)

    def reveal(self, key: str) -> None:
        """把目标滚动到可视区域（延迟一帧，等布局落定）。"""
        nav = self._nav
        if nav is None:
            return
        target = self._anchor_widget(key)
        if target is None:
            return
        QTimer.singleShot(
            0, lambda widget=target: self._ensure_visible(widget),
        )

    def focus_target(self, key: str) -> QWidget | None:
        """跳转焦点框围住的组件：叶子围编辑控件，分组围分组框。"""
        anchor = self._anchor_widget(key)
        if anchor is None:
            return None
        if isinstance(anchor, SettingRow):
            return anchor.control
        if isinstance(anchor, _UncoveredRow):
            return anchor.control
        return anchor

    def _anchor_widget(self, key: str) -> QWidget | None:
        nav = self._nav
        if nav is None:
            return None
        return dict(nav.visible_anchors).get(key) or self._row_or_box(key)

    def _row_or_box(self, key: str) -> QWidget | None:
        return self._group_boxes.get(key) or self._rows.get(key)

    def _ensure_visible(self, widget: QWidget) -> None:
        try:
            self._right_scroll.ensureWidgetVisible(widget, 0, 24)
        except RuntimeError:  # 重建后旧 widget 已销毁
            return

    def _set_anchor_highlight(self, key: str | None) -> None:
        nav = self._nav
        if nav is not None:
            nav.highlight(key)
        self.anchor_changed.emit(key)

    def _schedule_frame(self) -> None:
        """分组展开 / 折叠后重算虚线焦点框的位置。"""
        nav = self._nav
        if nav is not None:
            nav.schedule_frame()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802  Qt 覆写
        super().resizeEvent(event)
        self._schedule_frame()

    # ==================================================================
    # 提交：吐值 + 回填结果
    # ==================================================================

    def collect_values(self) -> dict[str, Any]:
        """**全量可编辑 binder** 的 ``{key: typed_value}``（不看当前范围）。"""
        return {
            key: binder.collect()
            for key, binder in self._binders.items()
            if binder.editable
        }

    def _emit_submit(self, mode: SubmitMode) -> None:
        values = self.collect_values()
        self._set_status(
            strings.SUBMIT_COUNT.format(mode=mode.text, count=len(values)),
            level="info",
        )
        self.submit_requested.emit(mode.value, values)

    def show_submit_result(self, result: Any) -> None:
        """把写回层的结果渲染回视图。

        * ``validation_errors[].key`` → 对应 binder → 行内 ``⚠`` + tooltip；
        * ``pending`` 非空 → footer「N 项未落盘」+ 重试按钮；
        * ``ok`` → footer「已保存」；
        * ``changes`` / ``dry_run`` → 预览表。
        """
        if result is None:
            return
        payload = (
            result.to_dict() if hasattr(result, "to_dict") else dict(result)
        )
        errors = list(payload.get("validation_errors") or [])
        for binder in self._binders.values():
            binder.show_error(None)
        unplaced: list[str] = []
        for error in errors:
            message = str(error.get("message") or strings.VALIDATION_FALLBACK)
            key = self._resolve_binder_key(error.get("key") or error.get("path"))
            binder = self._binders.get(key) if key else None
            if binder is None:
                unplaced.append(f"{error.get('key') or error.get('path')}: {message}")
            else:
                binder.show_error(message)

        pending_raw = list(payload.get("pending") or [])
        pending_keys = {
            key for key in (self._resolve_binder_key(item) for item in pending_raw)
            if key is not None
        }
        applied_keys = self._result_keys(
            list(payload.get("applied") or []) + list(payload.get("nodes") or []),
        )
        deferred = bool(payload.get("deferred"))
        if deferred:
            pending_keys |= applied_keys
        if not payload.get("dry_run"):
            for key in applied_keys:
                binder = self._binders.get(key)
                if binder is not None:
                    binder.mark_persisted(pending=key in pending_keys)
            for key in pending_keys - applied_keys:
                binder = self._binders.get(key)
                if binder is not None:
                    binder.mark_persisted(pending=True)

        self._pending_keys = pending_keys
        self._retry_button.setVisible(bool(pending_keys))
        self._update_dirty_label()

        if errors:
            message = strings.FOOTER_VALIDATION.format(count=len(errors))
            if unplaced:
                message += strings.FOOTER_UNPLACED.format(count=len(unplaced))
            self._footer_status_for(message, level="error")
            self._set_status(strings.STATUS_VALIDATION_FAILED, level="error")
        elif pending_keys:
            error_text = str(payload.get("error") or "").strip()
            detail = (
                strings.FOOTER_PENDING_DETAIL.format(detail=error_text)
                if error_text else ""
            )
            self._footer_status_for(
                strings.FOOTER_PENDING.format(count=len(pending_keys)) + detail,
                level="warning",
            )
            self._set_status(strings.STATUS_PENDING, level="warning")
        elif payload.get("dry_run"):
            changes = list(payload.get("changes") or [])
            changed_count = sum(1 for change in changes if change.get("changed"))
            self._footer_status_for(
                strings.FOOTER_PREVIEW.format(
                    changed=changed_count, total=len(changes),
                ),
                level="info",
            )
            self._set_status(strings.STATUS_PREVIEW_DONE, level="info")
        elif payload.get("ok", True) is False:
            self._footer_status_for(
                str(payload.get("error") or strings.FOOTER_SUBMIT_FAILED),
                level="error",
            )
            self._set_status(strings.STATUS_SUBMIT_FAILED, level="error")
        else:
            written = list(payload.get("written") or [])
            self._footer_status_for(
                strings.FOOTER_SAVED_FILES.format(count=len(written))
                if written else strings.FOOTER_SAVED,
                level="success",
            )
            self._set_status(strings.STATUS_SAVED, level="success")

        # 预览是「给用户看的表」，与校验错误可以同时出现：dry-run 的用途就是
        # 先看将写入什么，同时把问题标出来。
        if payload.get("dry_run"):
            self._show_preview(list(payload.get("changes") or []))

        if unplaced and not errors:
            self._footer_status_for("；".join(unplaced), level="error")

    def _footer_status_for(self, text: str, *, level: str) -> None:
        self._footer_status.setText(text)
        self._footer_status.setProperty("level", level)
        self._footer_status.style().unpolish(self._footer_status)
        self._footer_status.style().polish(self._footer_status)

    def _set_status(self, text: str, *, level: str = "info") -> None:
        self._status_label.setText(text)
        self._status_label.setProperty("level", level)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _show_preview(self, changes: list[Mapping[str, Any]]) -> None:
        if self._preview_dialog is not None:
            self._preview_dialog.close()
        dialog = SettingsPreviewDialog(changes, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(
            lambda _result, target=dialog: self._on_preview_closed(target),
        )
        self._preview_dialog = dialog
        dialog.show()

    def _on_preview_closed(self, dialog: SettingsPreviewDialog) -> None:
        if self._preview_dialog is dialog:
            self._preview_dialog = None

    def _resolve_binder_key(self, candidate: Any) -> str | None:
        """把多种键写法解析到 binder 的 qualname。

        支持 ``"group.item"``（binder key）、``"[section]key"``（dump 的
        ``path``）、``(section, key)``（通知 node）、以及裸叶子 name（仅在
        全局唯一时）。
        """
        if candidate is None:
            return None
        if isinstance(candidate, (tuple, list)) and len(candidate) == 2:
            keys = self._node_to_keys.get(
                (str(candidate[0]), str(candidate[1])),
            )
            return keys[0] if keys else None
        text = str(candidate)
        if text in self._binders:
            return text
        if text.startswith("[") and "]" in text:
            section, _, key = text[1:].partition("]")
            keys = self._node_to_keys.get((section, key))
            if keys:
                return keys[0]
        for key, binder in self._binders.items():
            if binder.leaf.get("path") == text:
                return key
        matches = [
            key for key, binder in self._binders.items()
            if binder.leaf.get("name") == text
        ]
        return matches[0] if len(matches) == 1 else None

    def _result_keys(self, values: Iterable[Any]) -> set[str]:
        keys: set[str] = set()
        for value in values:
            key = self._resolve_binder_key(value)
            if key is not None:
                keys.add(key)
        return keys

    # ==================================================================
    # 重建
    # ==================================================================

    def _build_content(self) -> None:
        """按当前树建**全部**控件（不按范围裁剪：范围只切可见性）。"""
        for row in self._rows.values():
            row.detach()
        self._clear_layout(self._right_layout)
        self._clear_layout(self._sidebar_layout)
        self._sidebar_layout.addStretch(1)
        self._group_boxes.clear()
        self._rows.clear()
        self._group_parent.clear()
        self._item_parent.clear()
        self._empty_hint = None
        if self._nav is not None:
            self._nav.clear()

        if self._tree is None:
            self._empty_hint = self._add_empty_hint(strings.WAITING_TREE)
            return

        for group in self._tree.groups:
            box = self._build_group(group, depth=0)
            if box is not None:
                self._right_layout.addWidget(box)
        self._build_unbound_section(expanded=True)
        self._build_uncovered_section(expanded=False)
        self._right_layout.addStretch(1)

    def _apply_visibility(self) -> None:
        """只切可见性：不销毁控件、不动 binder、不丢锚点选择以外的东西。

        范围（``current_scope``）与高级开关都会走到这里；整棵树安装时才走
        :meth:`_build_content`。
        """
        if self._tree is None:
            return

        # ``setVisible`` 会触发布局失效：只在状态真的变化时调，避免整页重排。
        for key, box in self._group_boxes.items():
            _set_visible_if_needed(box, self._group_box_visible(key))
        for key, row in self._rows.items():
            _set_visible_if_needed(row, self._row_visible(key))
        for key, box in self._group_boxes.items():
            group = self._tree.group(key)
            if group is not None:
                box.refresh_counts(
                    visible_count(group, self._current_scope, self._show_advanced),
                )

        nav = self._nav
        if nav is not None:
            nav.set_visible(
                {key: self._anchor_visible(key) for key in nav},
            )
        visible_anchors = dict(nav.visible_anchors) if nav is not None else {}

        if visible_anchors:
            if self._empty_hint is not None and self._tree is not None:
                self._empty_hint.setParent(None)
                self._empty_hint.deleteLater()
                self._empty_hint = None
        elif self._empty_hint is None:
            self._empty_hint = self._add_empty_hint(strings.NOTHING_IN_SCOPE)

        self._set_anchor_highlight(None)

    # ------------------------------------------------------------------
    # 可见性判定（范围 × 高级 × 合成分组）
    # ------------------------------------------------------------------

    def _group_box_visible(self, key: str) -> bool:
        scope = self._current_scope
        if key in (UNBOUND_GROUP_KEY, UNCOVERED_GROUP_KEY):
            return section_visible(key, scope)
        group = self._tree.group(key) if self._tree is not None else None
        if group is None:
            return False
        return group_visible(
            group, self._current_scope, self._show_advanced,
        )

    def _row_visible(self, key: str) -> bool:
        binder = self._binders.get(key)
        if binder is None:
            return False
        parent = self._item_parent.get(key)
        if parent in (UNBOUND_GROUP_KEY, UNCOVERED_GROUP_KEY):
            return self._group_box_visible(parent)
        if parent is not None and not self._group_box_visible(parent):
            return False
        return item_visible(
            binder.leaf, self._current_scope, self._show_advanced,
        )

    def _anchor_visible(self, key: str) -> bool:
        if key in self._group_boxes:
            return self._group_box_visible(key)
        if key in self._rows:
            return self._row_visible(key)
        if key.startswith(f"{UNCOVERED_GROUP_KEY}:"):
            return self._group_box_visible(UNCOVERED_GROUP_KEY)
        return True

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _add_empty_hint(self, text: str) -> QLabel:
        label = QLabel(text, self._right_content)
        label.setObjectName("SettingEmptyHint")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._right_layout.addWidget(label, 1)
        return label

    # ------------------------------------------------------------------
    # 正常分组树
    # ------------------------------------------------------------------

    def _build_group(
            self, group: Mapping[str, Any], *, depth: int,
    ) -> SettingGroupBox | None:
        count = visible_count(group, self._current_scope, self._show_advanced)
        box = SettingGroupBox(
            group,
            visible_count=count,
            depth=depth,
            expanded=count <= AUTO_COLLAPSE_ITEM_COUNT,
            parent=self._right_content,
        )
        self._group_boxes[box.group_key] = box
        self._group_parent[box.group_key] = None
        box.toggled.connect(lambda _checked: self._schedule_frame())
        self._register_anchor(
            box.group_key, box, str(group.get("title") or box.group_key),
            depth=depth, leaf=False,
        )
        for component in group.get("components") or ():
            widget = self._build_component(component)
            if widget is not None:
                box.content_layout.addWidget(widget)
        # 先占位记录父链：子分组在递归里设置真正的父 key
        for kind, entry in ordered_children(group):
            if kind == "item":
                row = self._make_row(entry["key"])
                if row is None:
                    continue
                self._item_parent[row.key] = box.group_key
                box.add_row_widget(row)
                self._register_anchor(
                    row.key, row, str(entry.get("label") or row.key),
                    depth=depth + 1, leaf=True,
                )
            else:
                child = self._build_group(entry, depth=depth + 1)
                if child is not None:
                    self._group_parent[child.group_key] = box.group_key
                    box.add_group_widget(child)
        return box

    def _build_component(
            self, component: Mapping[str, Any],
    ) -> QWidget | None:
        """渲染一个自定义组件（当前只有 ``label``）。"""
        text = str(component.get("text") or "")
        if not text:
            return None
        kind = str(component.get("type") or "label").lower()
        label = QLabel(text, self._right_content)
        label.setObjectName(
            str(component.get("object_name") or "SettingAboutText"),
        )
        label.setWordWrap(bool(component.get("word_wrap", True)))
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        if kind != "label":
            label.setText(
                strings.COMPONENT_UNSUPPORTED.format(kind=kind, text=text),
            )
        return label

    def _make_row(self, key: str) -> SettingRow | None:
        binder = self._binders.get(key)
        if binder is None:
            return None
        row = SettingRow(binder, self._right_content)
        self._rows[key] = row
        row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        row.customContextMenuRequested.connect(
            lambda pos, k=key: self._show_row_context_menu(k, pos),
        )
        self._share_context_menu(row, key)
        return row

    #: 这些子控件保留自己的右键菜单（复制 / 粘贴 / 下拉），不抢成行菜单。
    _NATIVE_MENU_TYPES = (
        QLineEdit,
        QPlainTextEdit,
        QComboBox,
        QAbstractSpinBox,
    )

    def _share_context_menu(self, row: SettingRow, key: str) -> None:
        """让整行都能右键出「移动到分组…」（而不是只有行尾空白处）。

        行的文本 / 徽标 / 容器本身不吃右键，于是"点哪都没反应"；这里把行内
        非输入类子控件都接管成同一条行菜单，输入控件保留原生菜单。
        """
        for child in row.findChildren(QWidget):
            if isinstance(child, self._NATIVE_MENU_TYPES):
                continue
            child.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            child.customContextMenuRequested.connect(
                lambda pos, k=key, w=child: self._show_row_context_menu(
                    k, w.mapTo(row, pos),
                ),
            )

    def _show_row_context_menu(self, key: str, pos: Any) -> None:
        """叶子行右键：位置调整（移动 / 恢复默认位置）。"""
        row = self._rows.get(key)
        if row is None:
            return
        menu = QMenu(row)
        move = menu.addAction(strings.MENU_MOVE_TO_GROUP)
        move.triggered.connect(lambda: self.move_requested.emit(key))
        reset = menu.addAction(strings.MENU_RESET_POSITION)
        reset.triggered.connect(lambda: self.reset_move_requested.emit(key))
        menu.exec(row.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # 未绑定 / 未覆盖（合成分组）
    # ------------------------------------------------------------------

    def _build_unbound_section(self, *, expanded: bool) -> None:
        if self._tree is None:
            return
        keys = list(self._tree.unbound)
        if not keys:
            return
        group = unbound_group(keys)
        box = SettingGroupBox(
            group, visible_count=len(keys), depth=0,
            expanded=expanded, parent=self._right_content,
        )
        self._group_boxes[UNBOUND_GROUP_KEY] = box
        self._group_parent[UNBOUND_GROUP_KEY] = None
        box.toggled.connect(lambda _checked: self._schedule_frame())
        self._register_anchor(
            UNBOUND_GROUP_KEY, box, strings.UNBOUND_TITLE, depth=0, leaf=False,
        )
        for key in keys:
            binder = self._binders.get(key)
            if binder is None:
                continue
            row = SettingRow(binder, self._right_content)
            self._rows[key] = row
            self._item_parent[key] = UNBOUND_GROUP_KEY
            box.add_row_widget(row)
            self._register_anchor(
                key, row, str(binder.leaf.get("label") or key),
                depth=1, leaf=True,
            )
        self._right_layout.addWidget(box)

    def _build_uncovered_section(self, *, expanded: bool) -> None:
        if self._tree is None:
            return
        entries = list(self._tree.uncovered)
        if not entries:
            return
        group = uncovered_group(entries)
        box = SettingGroupBox(
            group, visible_count=len(entries), depth=0,
            expanded=expanded, parent=self._right_content,
        )
        self._group_boxes[UNCOVERED_GROUP_KEY] = box
        self._group_parent[UNCOVERED_GROUP_KEY] = None
        box.toggled.connect(lambda _checked: self._schedule_frame())
        self._register_anchor(
            UNCOVERED_GROUP_KEY, box, strings.UNCOVERED_TITLE, depth=0, leaf=False,
        )
        for entry in entries:
            row = _UncoveredRow(entry, self._right_content)
            anchor = f"{UNCOVERED_GROUP_KEY}:{entry.get('path')}"
            self._item_parent[anchor] = UNCOVERED_GROUP_KEY
            box.add_row_widget(row)
            self._register_anchor(
                anchor, row, str(entry.get("path") or "?"), depth=1, leaf=True,
            )
        self._right_layout.addWidget(box)

    # ------------------------------------------------------------------
    # 侧栏锚点
    # ------------------------------------------------------------------

    def _register_anchor(
            self,
            key: str,
            widget: QWidget,
            text: str,
            *,
            depth: int,
            leaf: bool,
    ) -> None:
        nav = self._nav
        if nav is None:
            return
        nav.add(key, widget, text, depth=depth, leaf=leaf)

    # ==================================================================
    # 其它状态刷新
    # ==================================================================

    def _on_binder_event(self, _binder: LeafBinder, event: str) -> None:
        self._update_dirty_label()
        if event == "ui":
            self._set_status(strings.STATUS_DIRTY, level="warning")
        elif event == "reset":
            self._set_status(strings.STATUS_RESET, level="warning")

    def _update_dirty_label(self) -> None:
        count = sum(1 for binder in self._binders.values() if binder.dirty)
        self._dirty_label.setText(
            strings.DIRTY_COUNT.format(count=count) if count else "",
        )
        has_editable = any(binder.editable for binder in self._binders.values())
        self._preview_button.setEnabled(has_editable)
        self._save_button.setEnabled(has_editable)
        self._draft_button.setEnabled(has_editable)

    def _update_header(self) -> None:
        if self._tree is None:
            self._summary.setText(strings.HEADER_SUMMARY_EMPTY)
            return
        summary = self._tree.summary
        self._summary.setText(strings.HEADER_SUMMARY.format(
            groups=summary.get("group_count", 0),
            items=summary.get("item_count", 0),
            files=summary.get("file_count", 0),
            uncovered=summary.get("uncovered_count", 0),
        ))

    def _sync_scope_combo(self) -> None:
        with QSignalBlocker(self._scope_combo):
            self._scope_combo.clear()
            scopes = [Scope.all()]
            if self._tree is not None:
                for path in self._tree.by_file:
                    scopes.append(Scope.for_file(path))
            scopes.append(Scope.unbound())
            scopes.append(Scope.uncovered())
            for scope in scopes:
                self._scope_combo.addItem(scope.label, scope)
                self._scope_combo.setItemData(
                    self._scope_combo.count() - 1, scope.tooltip,
                    Qt.ItemDataRole.ToolTipRole,
                )
            current = 0
            for index in range(self._scope_combo.count()):
                if self._scope_combo.itemData(index) == self._current_scope:
                    current = index
                    break
            self._scope_combo.setCurrentIndex(current)
            if self._scope_combo.itemData(current) != self._current_scope:
                # 当前范围没有对应项（理论上 _validate_scope 已处理）→ 回退全部
                self._current_scope = Scope.all()
                self._scope_combo.setCurrentIndex(0)

    def _on_scope_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        scope = self._scope_combo.itemData(index)
        if isinstance(scope, Scope) and scope != self._current_scope:
            self.set_scope(scope)


class _UncoveredRow(QWidget):
    """未声明节点的一行（只读展示）。

    ``uncovered`` 的每条记录只有 ``value_repr``，没有 typed 值 / qualname，
    所以这里**不建 binder、不参与提交**。``typed=True`` 的节点用**只读输入框**
    展示（类型已知、但树外），``untyped`` 的用**裸文本区**展示；两者都禁用。
    """

    def __init__(
            self,
            entry: Mapping[str, Any],
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.entry: dict[str, Any] = dict(entry)
        self.setObjectName("SettingExtraRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)
        path = QLabel(str(entry.get("path") or "?"), self)
        path.setObjectName("SettingUncoveredPath")
        path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        meta_parts = ["typed" if entry.get("typed") else "untyped"]
        if entry.get("target_file"):
            meta_parts.append(f"file={entry['target_file']}")
        else:
            meta_parts.append(strings.HINT_NO_TARGET)
        if entry.get("origin_file"):
            meta_parts.append(f"origin={entry['origin_file']}")
        meta = QLabel(" · ".join(meta_parts), self)
        meta.setObjectName("SettingUncoveredMeta")
        text_box.addWidget(path)
        text_box.addWidget(meta)
        layout.addLayout(text_box, 1)

        text = str(entry.get("value_repr") or "")
        if entry.get("typed"):
            control: QWidget = QLineEdit(text, self)
            control.setReadOnly(True)
        else:
            control = QPlainTextEdit(text, self)
            control.setReadOnly(True)
            control.setFixedHeight(48)
        control.setEnabled(False)
        control.setToolTip(strings.TOOLTIP_UNCOVERED)
        control.setMinimumWidth(180)
        control.setMaximumWidth(460)
        self._control = control
        layout.addWidget(control, 0)

        lock = QLabel("🔒", self)
        lock.setObjectName("SettingRowLockIcon")
        lock.setFixedWidth(16)
        lock.setToolTip(strings.TOOLTIP_UNCOVERED_LOCK)
        layout.addWidget(lock, 0)

    @property
    def control(self) -> QWidget:
        """只读展示控件（跳转焦点框围住它）。"""
        return self._control


def _set_visible_if_needed(widget: QWidget, visible: bool) -> None:
    """``setVisible`` 的幂等版（同值不触发布局失效）。"""
    if widget.isHidden() == visible:
        widget.setVisible(visible)
