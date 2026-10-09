#
""""""

from __future__ import annotations

from typing import Sequence, Mapping, Optional, Any

from PySide6.QtCore import Qt, Signal, QModelIndex
from PySide6.QtWidgets import (
    QPushButton,
    QVBoxLayout,
    QListView,
    QWidget,
    QHBoxLayout,
    QGridLayout,
)

from ..delegate.square_thumb_delegate import SquareThumbDelegate

from ..model.navigation_bar_model import ThumbnailModel
from ..widgets import BlurLabel
from ..widgets.image_viewer import ImageViewer

from .utils import install_close_button
from .panel import InformationPanel


class Detail(BlurLabel):
    """照片详情页容器（继承自 BlurLabel，背景自动模糊）。

    契约（与 PresenterManager / ViewService 一致）：所有对外接口只走
    uuid 字符串；``assets.id`` 不再出现在本层。
    """
    close_requested = Signal()
    asset_navigate_requested = Signal(str)  # 参数：asset uuid

    # ---- 写操作意图（由内部按钮发出） ----
    favorite_toggle_requested = Signal(bool)  # payload: 期望的新收藏状态
    trash_requested = Signal()
    recover_requested = Signal()
    multiple_choice_requested = Signal()
    edit_requested = Signal()

    # ---- 对外广播：写操作已生效，需要外部（缩略图网格等）刷新 ----
    assets_changed = Signal(list)  # payload: list[str] (uuid)

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

    def __init__(self, parent=None):
        super().__init__(parent=parent, draw_label_content=False)
        self._nav_asset_uuids: list[str] = []
        self._nav_delegate: SquareThumbDelegate | None = None
        self._nav_model: ThumbnailModel | None = None

        # 上/下一张导航缓存（uuid；None 表示到头）
        self._nav_prev_uuid: Optional[str] = None
        self._nav_next_uuid: Optional[str] = None
        self._nav_index: int = 0
        self._nav_total: int = 0

        # 当前资产是否已软删；决定 trash 按钮语义为 删除 / 恢复
        self._is_deleted: bool = False

        self.setup_ui()
        self.setup_key()

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        # ---------- 顶部：关闭按钮 ----------
        self.close_button = install_close_button(self)
        self.close_button.setObjectName("DetailCloseButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)

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
        # self.overlay.setAttribute(
        #     Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        # )

        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(12, 12, 12, 12)
        overlay_layout.setSpacing(8)

        # 导航栏（横向缩略图）
        self.item_line_viewer = QListView(self.overlay)
        self.item_line_viewer.setFlow(QListView.Flow.LeftToRight)
        self.item_line_viewer.setWrapping(False)
        self.item_line_viewer.setUniformItemSizes(True)
        self.item_line_viewer.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.item_line_viewer.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # 按钮
        self.multiple_choice_button = QPushButton(self.tr("choice"), self.overlay)
        self.favourite_button = QPushButton(self.tr("favourite"), self.overlay)
        self.information_button = QPushButton(self.tr("info"), self.overlay)
        self.edit_button = QPushButton(self.tr("edit"), self.overlay)
        self.trash_button = QPushButton(self.tr("trash"), self.overlay)

        self.operation_container_widget = QWidget(self.overlay)
        self.operation_container_layout = QHBoxLayout(self.operation_container_widget)
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

        overlay_layout.addWidget(self.item_line_viewer)
        overlay_layout.addLayout(bottom_bar)

        self.content_layout.addWidget(
            self.overlay, 0, 0, Qt.AlignmentFlag.AlignBottom
        )

        # 收藏按钮做成 checkable，presenter 只负责 setChecked
        self.favourite_button.setCheckable(True)

        # 意图信号：视图只声明"被点了"，不含业务
        # favourite: clicked 携带 bool，直接透传作为"期望的新状态"
        self.favourite_button.clicked.connect(
            self.favorite_toggle_requested.emit
        )
        # trash: 依据当前 _is_deleted 路由到 trash / recover
        self.trash_button.clicked.connect(self._on_trash_clicked)
        self.multiple_choice_button.clicked.connect(
            self.multiple_choice_requested.emit
        )
        self.edit_button.clicked.connect(self.edit_requested.emit)

        # 导航栏几何
        self.item_line_viewer.setIconSize(ThumbnailModel.THUMB_SIZE)
        self.item_line_viewer.setGridSize(ThumbnailModel.THUMB_SIZE)
        self.item_line_viewer.setSpacing(4)
        self.item_line_viewer.setFixedHeight(
            ThumbnailModel.THUMB_SIZE.height() + 24
        )
        self.item_line_viewer.setResizeMode(QListView.ResizeMode.Adjust)
        self.item_line_viewer.setSelectionMode(
            QListView.SelectionMode.SingleSelection
        )

        self._nav_delegate = SquareThumbDelegate(self.item_line_viewer)
        self._nav_model = ThumbnailModel(parent=self)
        self.item_line_viewer.setModel(self._nav_model)
        # noinspection bad-argument-type
        self.item_line_viewer.setItemDelegate(self._nav_delegate)

        # 信息面板：挂到 Detail 自身，铺满整个详情页，不参与任何 layout
        self.panel = InformationPanel(self)  # 父 = Detail
        self.panel.setVisible(False)
        self.panel.setGeometry(self.rect())  # 与 Detail 完全重合

        self.information_button.setCheckable(True)
        self.information_button.toggled.connect(self._on_information_toggled)

        self.panel.close_requested.connect(
            lambda: self.information_button.setChecked(False)
        )

        # 点击走 model
        self.item_line_viewer.clicked.connect(self._on_nav_item_clicked)

    def setup_key(self):
        # 键盘总线尚未就绪，暂不接入；等统一键盘操作总线上线后在此挂载。
        pass

    # ------------------------------------------------------------------ #
    # 图片
    # ------------------------------------------------------------------ #
    def clear_image(self) -> None:
        self.image_viewer.clear()

    # ------------------------------------------------------------------ #
    # 导航栏
    # ------------------------------------------------------------------ #
    def set_navigation_items(
            self,
            *,
            asset_uuids: Sequence[str],
            thumb_map: Mapping[str, str],
            current_asset_uuid: str,
            include_deleted: bool = False,
            thumb_service: Optional[Any] = None,
    ) -> None:
        """重建底部导航缩略图栏。

        Args:
            asset_uuids: 当前相册中按契约排序的 uuid 列表。
            thumb_map: uuid -> 缩略图磁盘路径。
            current_asset_uuid: 需要选中的资产 uuid。
            include_deleted: Trash 场景传 True；会随生成任务下发给
                ``ThumbnailService``。
            thumb_service: 可选注入的 ``ThumbnailService``；首次传入后
                ``ThumbnailModel`` 会订阅其 ``thumbnail_ready`` 广播。
        """
        assert self._nav_model is not None

        if thumb_service is not None:
            self._nav_model.set_thumb_service(thumb_service)

        # 关键：不再 int()，直接以 uuid 字符串为准
        self._nav_asset_uuids = [str(u) for u in asset_uuids]
        self._nav_model.set_items(
            self._nav_asset_uuids,
            thumb_map,
            include_deleted=include_deleted,
        )

        current_uuid = str(current_asset_uuid)
        row = self._nav_model.row_of(current_uuid)
        if row < 0:
            return

        self._nav_model.set_current_index(row)

        idx = self._nav_model.index(row, 0)
        self.item_line_viewer.setCurrentIndex(idx)
        self.item_line_viewer.scrollTo(
            idx, QListView.ScrollHint.PositionAtCenter
        )

    def set_navigation_position(
            self,
            *,
            index: int,
            total: int,
            prev_uuid: Optional[str],
            next_uuid: Optional[str],
    ) -> None:
        """缓存翻页上下文（uuid）。

        - total == 0 / index == 0 时应禁用翻页；
        - prev_uuid / next_uuid 为 None 表示该方向已到头。
        """
        self._nav_index = int(index)
        self._nav_total = int(total)
        self._nav_prev_uuid = str(prev_uuid) if prev_uuid else None
        self._nav_next_uuid = str(next_uuid) if next_uuid else None

    # ------------------------------------------------------------------ #
    # 收藏 / 删除状态
    # ------------------------------------------------------------------ #
    def set_favorite_state(self, is_favorite: bool) -> None:
        """同步收藏按钮选中态。

        用 ``blockSignals`` 阻断回路：``setChecked`` 不触发 ``clicked``，
        但若未来改为监听 ``toggled``，也不会误发 ``favorite_toggle_requested``。
        """
        blocked = self.favourite_button.blockSignals(True)
        try:
            self.favourite_button.setChecked(bool(is_favorite))
        finally:
            self.favourite_button.blockSignals(blocked)

    def set_deleted_state(self, is_deleted: bool) -> None:
        """切换 trash 按钮语义：未删 → "trash"；已删 → "recover"。"""
        self._is_deleted = bool(is_deleted)
        self.trash_button.setText(
            self.tr("recover") if self._is_deleted else self.tr("trash")
        )

    def notify_assets_changed(self, asset_uuids: Sequence[str]) -> None:
        """写操作完成后由 presenter 调用，把受影响的资产 uuid 广播出去。

        外部的缩略图网格 / 收藏列表 / 搜索结果只需 connect ``assets_changed``，
        按 uuid 局部刷新即可。
        """
        self.assets_changed.emit([str(u) for u in asset_uuids])

    def show_information(self, visible: bool = True) -> None:
        """由 presenter 主动控制信息面板显隐（比如切换资产后重置）。"""
        self.information_button.setChecked(visible)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_nav_item_clicked(self, idx: QModelIndex) -> None:
        asset_uuid = self._nav_model.asset_id_at(idx.row())
        if asset_uuid is not None:
            self.asset_navigate_requested.emit(str(asset_uuid))

    def _on_trash_clicked(self) -> None:
        """统一入口：按当前 _is_deleted 路由到 trash / recover。"""
        if self._is_deleted:
            self.recover_requested.emit()
        else:
            self.trash_requested.emit()

    # ------------------------------------------------------------------ #
    # 信息面板：覆盖整页
    # ------------------------------------------------------------------ #
    def _on_information_toggled(self, checked: bool) -> None:
        if checked:
            self.panel.setGeometry(self.rect())
            self.panel.raise_()  # 抬到 Detail 里其它子控件之上
        self.panel.setVisible(checked)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Detail 尺寸变化时，面板要跟着铺满（前提是已经创建）
        if hasattr(self, "panel"):
            self.panel.setGeometry(self.rect())
