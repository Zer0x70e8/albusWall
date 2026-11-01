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
    QItemSelectionModel,
    QModelIndex,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
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
from ..widgets import BlurLabel
from ..widgets.image_viewer import ImageViewer

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
        return self.tr("恢复") if self._is_deleted else self.tr("移入回收站")

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
        self._apply_button_text(self.multiple_choice_button, self.tr("多选"))
        self._apply_button_text(self.favourite_button, self.tr("收藏"))
        self._apply_button_text(self.information_button, self.tr("信息"))
        self._apply_button_text(self.edit_button, self.tr("编辑"))
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
            self._hud_visible_before_preview = (
                self.overlay.isVisible(), self.close_button.isVisible())
            self.overlay.setVisible(False)
            self.close_button.setVisible(False)
            self._preview_mode = True
            self.preview_hint.setText(self._preview_hint_text())
            self.preview_hint.show()
            self.preview_hint.raise_()
            self._place_preview_hint()
            self._preview_hint_timer.start()
            return
        self._preview_mode = False
        self._preview_hint_timer.stop()
        self.preview_hint.hide()
        hud_visible, close_visible = self._hud_visible_before_preview
        self.overlay.setVisible(hud_visible)
        self.close_button.setVisible(close_visible)

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
        return self.tr("空格退出 · K 播放 / 暂停")

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
        if checked:
            self.panel.close_button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.information_button.setFocus(Qt.FocusReason.OtherFocusReason)

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
