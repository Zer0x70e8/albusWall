#
"""详情页容器：大图 + 底部 HUD（缩略图导航 / 标签栏 / 操作按钮）+ 信息面板。

纯视图组件，不查询数据、不碰 uuid / DTO：

* 所有按钮只发「被点了」的意图信号，具体动作由外部接线；
* 缩略图导航条只接受 ``(标识, 图标)`` 这样的展示数据；
* 信息面板接受 ``{键: 值}`` 字典，纯展示。
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Retranslatable
from ..model import NavigationModel
from ..model.roles import NavigationRole
from ..widgets import BlurLabel, ImageViewer

from .panel import InformationPanel
from .tagbar import TagBar
from .utils import install_close_button

__all__ = ["Detail"]

#: 底部导航缩略图的边长（与网格间距一起决定导航条高度）。
THUMB_SIZE = QSize(80, 80)

#: 导航条承载 chip 标识的 role。
#: 单一来源在 ``albuswall.ui.model.roles.NavigationRole``，这里只是别名。
NAV_ID_ROLE = int(NavigationRole.Id)


class Detail(Retranslatable, BlurLabel):
    """照片详情页容器（继承自 :class:`BlurLabel`，背景自动模糊）。"""

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    close_requested = Signal()
    asset_navigate_requested = Signal(str)    # 参数：导航项标识
    favorite_toggle_requested = Signal(bool)  # payload: 期望的新收藏状态
    trash_requested = Signal()
    recover_requested = Signal()
    multiple_choice_requested = Signal()
    edit_requested = Signal()

    # ---- 子控件声明（类型注解） ----
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

    panel: InformationPanel
    tag_bar: TagBar

    preview_hint: QLabel

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent=parent, draw_label_content=False)

        # 当前资产是否已软删；决定 trash 按钮语义为 删除 / 恢复
        self._is_deleted: bool = False

        # 快速预览模式：隐藏 HUD，只在底部留一条提示
        self._preview_mode: bool = False
        self._hud_visible_before_preview: tuple[bool, bool] = (True, True)
        # 进入预览前的焦点落点（只在焦点落本页子树内时记忆）
        self._focus_before_preview: QWidget | None = None
        # 进入预览前信息面板是否显示（退出预览时还原）
        self._panel_visible_before_preview: bool = False

        # 只显示图标的动作按钮：文案改放 tooltip（图标不参与翻译）
        self._icon_only: set[QPushButton] = set()

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)

        # ---------- 顶部：关闭按钮 ----------
        self.close_button = install_close_button(
            self, object_name="DetailCloseButton")
        self.close_button.clicked.connect(self.close_requested.emit)
        self.main_layout.addWidget(self.close_button)

        # ---------- 内容区：图片铺满 + 底部叠加层 ----------
        content = QWidget(self)
        self.main_layout.addWidget(content, 1)

        self.content_layout = QGridLayout(content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        self.image_viewer = ImageViewer(content)
        # 预览模式的空格 / 回车出口（见 eventFilter）
        self.image_viewer.installEventFilter(self)
        self.content_layout.addWidget(self.image_viewer, 0, 0)

        self.overlay = QWidget(content)
        self.overlay.setObjectName("DetailOverlay")

        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(12, 12, 12, 12)
        overlay_layout.setSpacing(8)

        # 导航栏（横向缩略图）
        self.item_line_viewer = QListView(self.overlay)
        self.item_line_viewer.setFlow(QListView.Flow.LeftToRight)
        self.item_line_viewer.setWrapping(False)
        self.item_line_viewer.setUniformItemSizes(True)
        self.item_line_viewer.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.item_line_viewer.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.item_line_viewer.setIconSize(THUMB_SIZE)
        self.item_line_viewer.setGridSize(THUMB_SIZE)
        self.item_line_viewer.setSpacing(4)
        self.item_line_viewer.setFixedHeight(THUMB_SIZE.height() + 24)
        self.item_line_viewer.setResizeMode(QListView.ResizeMode.Adjust)
        self.item_line_viewer.setSelectionMode(
            QListView.SelectionMode.SingleSelection)

        # 导航数据：标识 + 缩略图，不认识任何业务模型
        self._nav_model = NavigationModel(
            icon_size=THUMB_SIZE, parent=self)
        self.item_line_viewer.setModel(self._nav_model)
        self.item_line_viewer.clicked.connect(self._on_nav_item_clicked)
        # 键盘：方向键移动 + Enter/Return 激活（与点击同一条出口）
        self.item_line_viewer.activated.connect(self._on_nav_item_clicked)

        # 标签栏
        self.tag_bar = TagBar(self)

        # 操作按钮（文案由 retranslate_ui() 统一填）
        self.multiple_choice_button = QPushButton("", self.overlay)
        self.favourite_button = QPushButton("", self.overlay)
        self.information_button = QPushButton("", self.overlay)
        self.edit_button = QPushButton("", self.overlay)
        self.trash_button = QPushButton("", self.overlay)

        self.operation_container_widget = QWidget(self.overlay)
        self.operation_container_layout = QHBoxLayout(
            self.operation_container_widget)
        self.operation_container_layout.setContentsMargins(0, 0, 0, 0)
        self.operation_container_layout.setSpacing(8)
        self.operation_container_layout.addWidget(self.favourite_button)
        self.operation_container_layout.addWidget(self.information_button)
        self.operation_container_layout.addWidget(self.edit_button)

        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 0, 0, 0)
        bottom_bar.setSpacing(8)
        bottom_bar.addWidget(self.multiple_choice_button)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.operation_container_widget)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.trash_button)

        overlay_layout.addWidget(self.tag_bar)
        overlay_layout.addWidget(self.item_line_viewer)
        overlay_layout.addLayout(bottom_bar)

        self.content_layout.addWidget(
            self.overlay, 0, 0, Qt.AlignmentFlag.AlignBottom)

        # 收藏按钮做成 checkable，外部只负责 setChecked
        self.favourite_button.setCheckable(True)
        self.information_button.setCheckable(True)

        # ---- 内部连线：视图只声明「被点了」，不含业务 ----
        self.favourite_button.clicked[bool].connect(
            self.favorite_toggle_requested.emit)
        self.trash_button.clicked.connect(self._on_trash_clicked)
        self.multiple_choice_button.clicked.connect(
            self.multiple_choice_requested.emit)
        self.edit_button.clicked.connect(self.edit_requested.emit)
        self.information_button.toggled.connect(self._on_information_toggled)

        # 信息面板：覆盖整个详情页，不参与主布局
        self.panel = InformationPanel(self)
        self.panel.setVisible(False)
        self.panel.setGeometry(self.rect())
        self.panel.close_requested.connect(
            lambda: self.information_button.setChecked(False))
        # 面板可见期间把 Tab 关在面板内（见 eventFilter）：Qt 的 Tab 只认焦点链，
        # 面板兄弟层（标题栏 / 内容层）仍然可聚焦，必须自己拦。
        for widget in (self.panel.close_button, self.panel.view):
            widget.installEventFilter(self)

        # 快速预览模式的底部提示（不进 layout、不抢焦点）
        self.preview_hint = QLabel("", self)
        self.preview_hint.setObjectName("DetailPreviewHint")
        self.preview_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_hint.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview_hint.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.preview_hint.hide()
        self._preview_hint_timer = QTimer(self)
        self._preview_hint_timer.setSingleShot(True)
        self._preview_hint_timer.setInterval(4000)
        self._preview_hint_timer.timeout.connect(self.preview_hint.hide)

    # ------------------------------------------------------------------ #
    # 图片
    # ------------------------------------------------------------------ #
    def clear_image(self) -> None:
        """清空大图。"""
        self.image_viewer.clear()

    def set_image(self, pixmap: QPixmap | None) -> None:
        """显示一张图片；``None`` 表示清空。"""
        self.image_viewer.set_pixmap(pixmap)

    # ---- 缩放（快捷键 view.zoom_in / view.zoom_out / view.reset 的落点） ----
    def zoom_in(self) -> bool:
        """放大一档；返回视图是否真的变了。"""
        return self.image_viewer.zoom_in()

    def zoom_out(self) -> bool:
        """缩小一档；返回视图是否真的变了。"""
        return self.image_viewer.zoom_out()

    def reset_zoom(self) -> bool:
        """恢复「适应窗口」；返回视图是否真的变了。"""
        return self.image_viewer.zoom_fit()

    # ------------------------------------------------------------------ #
    # 导航栏（纯展示）
    # ------------------------------------------------------------------ #
    def set_navigation_items(
            self,
            items: Iterable[tuple[str, QIcon | QPixmap | None]],
            *,
            current: str | None = None,
    ) -> None:
        """重建底部导航缩略图栏。

        :param items: ``(标识, 图标)`` 序列；图标可为 ``QIcon`` / ``QPixmap`` /
            ``None``（占位）。标识只用于回传信号，本组件不解释它。
        :param current: 需要选中的标识；找不到时保持不动。
        """
        model = self._nav_model
        if hasattr(model, "set_items"):
            model.set_items(items)
        self._select_navigation(current)

    def set_navigation_model(self, model) -> None:
        """直接注入导航条模型（通常是 :class:`NavigationModel`）。

        注入后 :meth:`set_navigation_items` 会转调模型的 ``set_items``，
        懒加载（``set_lazy_provider``）这类能力也随之可用。
        """
        self._nav_model = model
        self.item_line_viewer.setModel(model)

    def navigation_model(self):
        """当前导航条模型。"""
        return self._nav_model

    def _select_navigation(self, current: str | None) -> None:
        """把 ``current`` 对应的行设为当前项并滚到中间。"""
        row = self._nav_row_of(current) if current is not None else -1
        if row < 0:
            return

        index = self._nav_model.index(row, 0)
        self.item_line_viewer.setCurrentIndex(index)
        # setCurrentIndex 不改选择：键盘方向键要从当前图继续，且要有可见高亮
        selection = self.item_line_viewer.selectionModel()
        if selection is not None:
            selection.select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        self.item_line_viewer.scrollTo(
            index, QListView.ScrollHint.PositionAtCenter)

    def clear_navigation(self) -> None:
        """清空底部导航栏（模型没有 ``set_items`` 时退回 ``clear``）。"""
        model = self._nav_model
        if hasattr(model, "set_items"):
            model.set_items([])
        elif hasattr(model, "clear"):
            model.clear()

    def _nav_row_of(self, nav_id: str) -> int:
        """标识对应的行号；找不到返回 ``-1``。"""
        model = self._nav_model
        if hasattr(model, "row_of"):
            return int(model.row_of(nav_id))
        target = str(nav_id)
        for row in range(model.rowCount()):
            data = model.index(row, 0).data(NAV_ID_ROLE)
            if data is not None and str(data) == target:
                return row
        return -1

    def _on_nav_item_clicked(self, index: QModelIndex) -> None:
        nav_id = index.data(NAV_ID_ROLE) if index.isValid() else None
        if nav_id is not None:
            self.asset_navigate_requested.emit(str(nav_id))

    # ------------------------------------------------------------------ #
    # 数据接口（直接接收模型）
    # ------------------------------------------------------------------ #
    def set_metadata(self, metadata) -> None:
        """装载信息面板的键值数据（``Mapping`` / dataclass / DTO 均可）。"""
        self.panel.set_metadata(metadata)

    def set_metadata_model(self, model, delegate=None) -> None:
        """直接注入信息面板的键值行模型。"""
        self.panel.set_model(model, delegate)

    def set_tag_models(self, chip_model=None, tree_model=None) -> None:
        """直接注入标签栏的 chip 模型与标签树模型。"""
        self.tag_bar.bind_models(chip_model, tree_model)

    # ------------------------------------------------------------------ #
    # 展示状态
    # ------------------------------------------------------------------ #
    def set_favorite_state(self, is_favorite: bool) -> None:
        """同步收藏按钮选中态（不触发 ``favorite_toggle_requested``）。"""
        blocked = self.favourite_button.blockSignals(True)
        try:
            self.favourite_button.setChecked(bool(is_favorite))
        finally:
            self.favourite_button.blockSignals(blocked)

    def set_deleted_state(self, is_deleted: bool) -> None:
        """切换 trash 按钮语义：未删 → "trash"；已删 → "recover"。"""
        self._is_deleted = bool(is_deleted)
        self._apply_button_text(self.trash_button, self._trash_button_text())

    def _trash_button_text(self) -> str:
        return self.tr("Restore") if self._is_deleted else self.tr("Move to Trash")

    def _on_trash_clicked(self) -> None:
        """统一入口：按当前展示状态路由到 trash / recover 意图。"""
        if self._is_deleted:
            self.recover_requested.emit()
        else:
            self.trash_requested.emit()

    def show_information(self, visible: bool = True) -> None:
        """外部主动控制信息面板显隐。"""
        self.information_button.setChecked(bool(visible))

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用详情页所有按钮文案（含 trash 的双态文案）。"""
        self._apply_button_text(self.multiple_choice_button, self.tr("Select"))
        self._apply_button_text(self.favourite_button, self.tr("Favorite"))
        self._apply_button_text(self.information_button, self.tr("Info"))
        self._apply_button_text(self.edit_button, self.tr("Edit"))
        self._apply_button_text(self.trash_button, self._trash_button_text())
        self.preview_hint.setText(self._preview_hint_text())

    def set_action_icon(self, button: QPushButton, icon: QIcon | str | None) -> None:
        """给动作按钮装图标（``None`` = 回到纯文字模式）。

        装了图标的按钮进入「只有图标」模式：文字清空、文案转 tooltip ——
        48px 的圆形按钮放不下「多选 / 收藏 / 信息」这类中文标签。
        """
        if icon is None or str(icon) == "":
            self._icon_only.discard(button)
            button.setIcon(QIcon())
            button.setToolTip("")
            self.retranslate_ui()
            return
        label = button.toolTip() or button.text()
        self._icon_only.add(button)
        button.setIcon(QIcon(str(icon)))
        self._apply_button_text(button, label)

    def is_action_icon_mode(self, button: QPushButton) -> bool:
        """该动作按钮当前是否是「只有图标」模式。"""
        return button in self._icon_only

    def _apply_button_text(self, button: QPushButton, text: str) -> None:
        """按按钮模式写文案：纯文字模式写 setText，图标模式写 tooltip。"""
        if button in self._icon_only:
            button.setText("")
            button.setToolTip(text)
            return
        button.setText(text)

    # ------------------------------------------------------------------ #
    # 快速预览模式（显式模式，不是宏）
    # ------------------------------------------------------------------ #
    @property
    def is_preview_mode(self) -> bool:
        """当前是否处于快速预览模式（HUD 已隐藏）。"""
        return self._preview_mode

    def set_preview_mode(self, active: bool) -> None:
        """进入 / 退出快速预览模式：隐藏或恢复 HUD，并显示底部提示。"""
        active = bool(active)
        if active == self._preview_mode:
            if active:
                self._place_preview_hint()
            return
        if active:
            # 用 ``isHidden`` 而不是 ``isVisible``：后者连带父链一起看，页面
            # 还没显示（例如窗口未 show、单测直接构造）时会把「本来可见」
            # 误记成 False，退出预览就再也恢复不出来。
            self._hud_visible_before_preview = (
                not self.overlay.isHidden(), not self.close_button.isHidden())
            # 只在焦点确实落在本页子树里时记忆，避免退出预览时把别处的焦点抢过来
            app = QApplication.instance()
            focused = app.focusWidget() if isinstance(app, QApplication) else None
            self._focus_before_preview = (
                focused if focused is not None
                and (focused is self or self.isAncestorOf(focused)) else None)
            # 预览是「只看图」：信息面板也要收起，退出时按进入前状态还原。
            # 走 information_button 而不是直接 setVisible，让「按钮 checked」
            # 与「面板可见」始终一致，也复用 _on_information_toggled 里的
            # enabled 围栏，避免出现「面板藏了但整页还是禁用」的中间态。
            self._panel_visible_before_preview = not self.panel.isHidden()
            if self._panel_visible_before_preview:
                self.information_button.setChecked(False)
            self.overlay.setVisible(False)
            self.close_button.setVisible(False)
            self._preview_mode = True
            self.preview_hint.setText(self._preview_hint_text())
            self.preview_hint.show()
            self.preview_hint.raise_()
            self._place_preview_hint()
            self._preview_hint_timer.start()
            self.image_viewer.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self._preview_mode = False
        self._preview_hint_timer.stop()
        self.preview_hint.hide()
        hud_visible, close_visible = self._hud_visible_before_preview
        self.overlay.setVisible(hud_visible)
        self.close_button.setVisible(close_visible)
        panel_was_visible = self._panel_visible_before_preview
        self._panel_visible_before_preview = False
        if panel_was_visible:
            self.information_button.setChecked(True)  # 走统一路径：几何 + raise + 围栏
        target = self._focus_before_preview
        self._focus_before_preview = None
        if target is None or not target.isVisible() or not target.isEnabled():
            target = (self.panel.close_button if self.panel.isVisible()
                      else self.image_viewer)
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_preview_hint(self, text: str) -> None:
        """覆盖快速预览底部提示。"""
        self.preview_hint.setText(str(text))
        if self._preview_mode:
            self.preview_hint.adjustSize()
            self._place_preview_hint()
            self.preview_hint.show()
            self.preview_hint.raise_()
            self._preview_hint_timer.start()

    def _preview_hint_text(self) -> str:
        return self.tr("Space to exit · K to play / pause")

    def _place_preview_hint(self) -> None:
        """提示条贴在底部居中；不参与 layout，随 resize 重算。"""
        hint = self.preview_hint
        hint.adjustSize()
        size = hint.sizeHint()
        hint.resize(size)
        x = max(0, (self.width() - size.width()) // 2)
        y = max(0, self.height() - size.height() - 16)
        hint.move(x, y)

    # ------------------------------------------------------------------ #
    # 信息面板：覆盖整页
    # ------------------------------------------------------------------ #
    def _on_information_toggled(self, checked: bool) -> None:
        if checked:
            self.panel.setGeometry(self.rect())
            self.panel.raise_()  # 抬到 Detail 里其它子控件之上
        self.panel.setVisible(checked)
        # 面板是整页覆盖层：可见期间被它遮住的控件必须退出键盘交互。
        # 光挪焦点不够 —— 「Tab 到被遮住的按钮 + 空格」照样能激活它，
        # 只有 setEnabled(False) 才把整棵子树同时移出焦点链与输入路径。
        for widget in (self.overlay, self.close_button, self.image_viewer):
            widget.setEnabled(not checked)
        if checked:
            self.panel.close_button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.information_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        """预览模式下的空格 / 回车；面板可见期间把 Tab 关在面板内部。

        快速预览（空格进入）里：

        * **空格** = 退出预览（收起详情页，回到网格）——与提示条一致；
        * **回车** = 升级成完整详情页（显示 HUD，留在详情页）。

        两条出口都与内容网格的约定对齐：空格是预览、回车是详情。

        ``setEnabled(False)`` 只能摘掉 Detail 自己的子树；标题栏与内容层是
        面板的兄弟层级，仍然留在焦点链上，Tab 会从面板里跑出去。这里只在
        面板可见时接管面板内控件的 Tab，其余事件照旧交给基类（模糊层要靠
        基类的事件过滤跟尺寸）。
        """
        if (self._preview_mode
                and obj is self.image_viewer
                and event.type() == QEvent.Type.KeyPress
                and not event.modifiers() & (
                        Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.AltModifier
                        | Qt.KeyboardModifier.MetaModifier)):
            key = event.key()
            if key == Qt.Key.Key_Space:
                self.set_preview_mode(False)
                self.close_requested.emit()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.set_preview_mode(False)
                return True

        panel = getattr(self, "panel", None)
        if (panel is not None
                and panel.isVisible()
                and panel.isAncestorOf(obj)
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
            other = (panel.view if obj is panel.close_button
                     else panel.close_button)
            reason = (Qt.FocusReason.BacktabFocusReason
                      if event.key() == Qt.Key.Key_Backtab
                      else Qt.FocusReason.TabFocusReason)
            other.setFocus(reason)
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    # 显示
    # ------------------------------------------------------------------ #
    def showEvent(self, event):  # noqa: N802 - Qt 命名
        """详情页重新显示时，若信息面板正开着，焦点给面板内的关闭按钮。

        P1 的 ``setEnabled(False)`` 会禁用 overlay / image_viewer，导致
        presenter 的 ``image_viewer.setFocus()`` 静默失败。showEvent 在
        ``presenter._on_overlay_changed`` 之前触发，先把焦点放到
        ``panel.close_button``，presenter 后续的 ``setFocus(image_viewer)``
        会因 disabled 静默失败，于是焦点保持在这里 —— 符合"面板开着就让
        面板接住焦点"的意图。
        """
        super().showEvent(event)
        if self.panel.isVisible():
            self.panel.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def hideEvent(self, event):  # noqa: N802 - Qt 命名
        """详情页被收起时退出快速预览。

        预览是「页内临时状态」：离开这一页（换浮层页 / 收起浮层）必须把
        HUD 与提示条还原，否则下次用回车打开详情时会看到缺了 HUD 的残局。
        """
        super().hideEvent(event)
        if self._preview_mode:
            self.set_preview_mode(False)

    # ------------------------------------------------------------------ #
    # 几何
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        # Detail 尺寸变化时，面板要跟着铺满（前提是已经创建）
        if hasattr(self, "panel"):
            self.panel.setGeometry(self.rect())
        if getattr(self, "preview_hint", None) is not None:
            self._place_preview_hint()
