#
""""""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, Mapping, Optional

from PySide6.QtGui import QPixmap
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
# from albuswall.dto.thumbnail import ThumbSpec

from ..model.navigation_bar_model import ThumbnailModel
from ..widgets import BlurLabel
from ..widgets.image_viewer import ImageViewer

from .utils import install_close_button


class Detail(BlurLabel):
    """照片详情页容器（继承自 BlurLabel，背景自动模糊）。"""
    close_requested = Signal()
    image_loaded = Signal(str)
    asset_navigate_requested = Signal(int)

    # ---- 写操作意图（由内部按钮发出） ----
    favorite_toggle_requested = Signal()
    trash_requested = Signal()
    multiple_choice_requested = Signal()
    edit_requested = Signal()

    # ---- 对外广播：写操作已生效，需要外部（缩略图网格等）刷新 ----
    assets_changed = Signal(list)  # payload: list[int]

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

    def __init__(self, parent=None):
        super().__init__(parent=parent, draw_label_content=False)
        self._nav_asset_ids: list[int] = []
        self._nav_model: ThumbnailModel | None = None

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
        self.main_layout.addWidget(content, 1)  # stretch=1，吃掉剩余全部空间

        self.content_layout = QGridLayout(content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        # 第 1 层：图片视图，铺满整个 content
        self.image_viewer = ImageViewer(content)
        self.content_layout.addWidget(self.image_viewer, 0, 0)

        # 第 2 层：底部叠加层（导航栏 + 按钮行）
        self.overlay = QWidget(content)
        self.overlay.setObjectName("DetailOverlay")  # 想加半透明背景就给它写 QSS
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

        # 三个按钮装进容器
        self.operation_container_widget = QWidget(self.overlay)
        self.operation_container_layout = QHBoxLayout(self.operation_container_widget)
        self.operation_container_layout.setContentsMargins(0, 0, 0, 0)
        self.operation_container_layout.setSpacing(8)
        self.operation_container_layout.addWidget(self.favourite_button)
        self.operation_container_layout.addWidget(self.information_button)
        self.operation_container_layout.addWidget(self.edit_button)

        # 底部按钮行：[choice] --- [容器] --- [trash]
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

        # 关键：叠加层放在同一个 (0,0) 单元格，只占它自己的高度，贴底对齐
        self.content_layout.addWidget(
            self.overlay, 0, 0, Qt.AlignmentFlag.AlignBottom
        )

        #
        self.item_line_viewer.clicked.connect(self._on_nav_item_clicked)

        # 收藏按钮做成 checkable，presenter 只负责 setChecked
        self.favourite_button.setCheckable(True)

        # 意图信号：视图只声明“被点了”，不含业务
        self.favourite_button.clicked.connect(
            self.favorite_toggle_requested.emit
        )
        self.trash_button.clicked.connect(self.trash_requested.emit)
        self.multiple_choice_button.clicked.connect(
            self.multiple_choice_requested.emit
        )
        self.edit_button.clicked.connect(self.edit_requested.emit)

        # 让每格就是「方形」的：iconSize 决定 pixmap 绘制尺寸，
        # gridSize 决定每格占位（含 padding），保持一致就能形成规整方格。
        self.item_line_viewer.setIconSize(ThumbnailModel.THUMB_SIZE)  # 80×80
        self.item_line_viewer.setGridSize(ThumbnailModel.THUMB_SIZE)
        self.item_line_viewer.setSpacing(4)

        # 固定高度，避免导航栏抢走竖直空间、也避免横向滚动条把格子压扁
        self.item_line_viewer.setFixedHeight(
            ThumbnailModel.THUMB_SIZE.height() + 24
        )
        self.item_line_viewer.setResizeMode(QListView.ResizeMode.Adjust)
        self.item_line_viewer.setSelectionMode(
            QListView.SelectionMode.SingleSelection
        )

        self._nav_delegate = SquareThumbDelegate(self.item_line_viewer)
        # ---------- 新增：把 ThumbnailModel 挂上 ----------
        self._nav_model = ThumbnailModel(parent=self)
        self.item_line_viewer.setModel(self._nav_model)

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

        # 点击走 model，别再用 self._nav_asset_ids（那是 None）
        self.item_line_viewer.clicked.connect(self._on_nav_item_clicked)

    def setup_key(self):
        # # ---------- Esc 关闭 ----------
        # sc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        # sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        # sc.activated.connect(self.close_button.clicked.emit)
        pass

    def set_image(self, path: str | Path) -> bool:
        """接收图片路径（可直接 connect 到携带 str 的信号）。"""
        pixmap = QPixmap()
        path = str(path)
        ok = bool(path) and pixmap.load(path)
        self.image_viewer.set_pixmap(pixmap if ok else QPixmap())
        if ok:
            self.image_loaded.emit(path)
        return ok

    def clear_image(self) -> None:
        self.image_viewer.clear()

    def set_navigation_items(
            self,
            *,
            asset_ids: Sequence[int],
            thumb_map: Mapping[int, str],
            current_asset_id: int,
    ) -> None:
        assert self._nav_model is not None

        self._nav_asset_ids = [int(a) for a in asset_ids]
        self._nav_model.set_items(self._nav_asset_ids, thumb_map)

        row = self._nav_model.row_of(current_asset_id)
        if row < 0:
            return

        self._nav_model.set_current_index(row)

        # 真正让 QSS 的 QListView::item:selected 生效、并滚到可见位置
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
            prev_id: Optional[int],
            next_id: Optional[int],
    ) -> None:
        """缓存 (index, total, prev_id, next_id)，供状态栏 / 方向键翻页使用。

        - total == 0 / index == 0 时应禁用翻页；
        - prev_id / next_id 为 None 表示该方向已到头。
        """

    def set_metadata(self, metadata: Mapping[str, object]) -> None:
        """用元信息 dict 刷新信息面板（尺寸、时间、EXIF 等）。"""

    def set_favorite_state(self, is_favorite: bool) -> None:
        """同步收藏按钮选中态（由 presenter 在切换后 / 打开详情时调用）。"""
        self.favourite_button.setChecked(bool(is_favorite))

    def notify_assets_changed(self, asset_ids: Sequence[int]) -> None:
        """写操作完成后由 presenter 调用，把受影响的资产 id 广播出去。

        外部的缩略图网格 / 收藏列表 / 搜索结果只需 connect ``assets_changed``，
        按 id 局部刷新即可。
        """
        self.assets_changed.emit([int(i) for i in asset_ids])

    def _on_nav_item_clicked(self, idx: QModelIndex) -> None:
        asset_id = self._nav_model.asset_id_at(idx.row())
        if asset_id is not None:
            self.asset_navigate_requested.emit(int(asset_id))

    # alias
    show_image = set_image
