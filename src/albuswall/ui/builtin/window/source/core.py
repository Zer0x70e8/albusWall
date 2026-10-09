#
""""""

from PySide6.QtCore import Qt, Signal, QModelIndex, QTimer, QPoint
from PySide6.QtWidgets import (
    QHBoxLayout, QSizePolicy, QWidget,
    QPushButton, QScrollArea, QSplitter, QVBoxLayout
)

from .detail_widget import IngestSourceDetailWidget
from ...widgets import BlurLabel, ColumnListView


class IngestSource(BlurLabel):
    # 卡片右键，抛出 (index, 全局坐标)
    context_menu_requested = Signal(QModelIndex, QPoint)
    # 点击卡片后向外抛出 QModelIndex
    card_clicked = Signal(QModelIndex)
    detail_closed = Signal()

    main_layout: QVBoxLayout
    splitter: QSplitter

    #
    content_layout: QHBoxLayout
    add_button_layout: QHBoxLayout
    close_button: QPushButton
    apply_button: QPushButton
    add_button: QPushButton

    #
    view: ColumnListView

    #
    detail_container: QWidget
    detail_layout: QVBoxLayout

    close_detail_btn_layout: QHBoxLayout
    close_detail_btn: QPushButton

    detail_widget_container: QWidget
    detail_widget: IngestSourceDetailWidget
    detail_widget_container_layout: QVBoxLayout

    scroll_area: QScrollArea

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("draw_label_content", False)
        super().__init__(*args, **kwargs)

        self.delegate = None
        self.model = None
        self._detail_mode = False
        self._selection_connected = False

        self.setObjectName("IngestSource")

        self._setup_ui()

    # ------------------------------------------------------------------ UI
    def _setup_ui(self):
        # ---------- 顶层 ----------
        self.main_layout = QVBoxLayout(self)

        # ---------- 按钮 ----------
        self.close_button = QPushButton("\u2715")
        self.apply_button = QPushButton(self.tr("apply"))
        self.add_button = QPushButton(self.tr("add"))

        # ---------- content ----------
        self.content_layout = QHBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        # ---- splitter ----
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)

        # ---- 左侧 view ----
        self.view = ColumnListView()
        self.view.set_column_count(2)
        self.view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        # ★ 右键菜单策略
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(
            self._on_view_context_menu_requested
        )

        self.view.clicked.connect(self._on_card_clicked)
        # if hasattr(self.view, "selectionModel"):
        #     self.view.selectionModel().currentChanged.connect(self._on_current_changed)
        # else:
        #     self.view.clicked.connect(self._on_card_clicked)

        self.splitter.addWidget(self.view)

        # ---- 右侧 detail ----
        self.detail_container = QWidget()
        self.detail_container.setMinimumWidth(200)
        self.detail_container.hide()

        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(0)

        # self.close_detail_btn_layout = QHBoxLayout()
        # self.close_detail_btn_layout.setContentsMargins(8, 8, 8, 0)

        self.close_detail_btn = QPushButton("\u2715")
        self.close_detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_detail_btn.clicked.connect(self._on_close_detail)

        # self.close_detail_btn_layout.addStretch(1)
        # self.close_detail_btn_layout.addWidget(self.close_detail_btn)

        self.detail_widget_container = QWidget()
        self.detail_widget = IngestSourceDetailWidget()
        self.detail_widget_container_layout = QVBoxLayout(self.detail_widget_container)
        self.detail_widget_container_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_widget_container_layout.addWidget(self.detail_widget)
        self.detail_widget_container_layout.addStretch()

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(self.detail_widget_container)

        # self.detail_layout.addLayout(self.close_detail_btn_layout)
        self.detail_layout.addWidget(self.close_detail_btn)
        self.detail_layout.addWidget(self.apply_button)
        self.detail_layout.addWidget(self.scroll_area, 1)

        self.splitter.addWidget(self.detail_container)
        self.splitter.setSizes([800, 360])

        self.content_layout.addWidget(self.splitter)

        # ---------- 顶部 add 按钮 ----------
        self.add_button_layout = QHBoxLayout()
        self.add_button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_button_layout.setSpacing(0)

        self.add_button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_button_layout.addStretch()
        self.add_button_layout.addWidget(self.add_button)

        # ---------- 组装 ----------
        self.main_layout.addWidget(self.close_button)
        self.main_layout.addLayout(self.add_button_layout)
        self.main_layout.addLayout(self.content_layout, 1)
        # 让 splitter 区域吸收伸缩

    # -------------------------------------------------------------- 数据接口
    def set_model(self, model, delegate):
        """设置数据模型和委托，供外部调用"""
        self.model = model
        self.delegate = delegate
        self.view.setModel(model)  # ★ 此时 selectionModel 才存在
        self.view.setItemDelegate(delegate)

        # 现在再连 currentChanged 才安全
        sm = self.view.selectionModel()
        if sm is not None and not self._selection_connected:
            sm.currentChanged.connect(self._on_current_changed)
            self._selection_connected = True

    # -------------------------------------------------------------- 交互
    # noinspection PyUnusedLocal
    def _on_card_clicked(self, index: QModelIndex):
        # 首次点击卡片时进入"详情模式"
        if not index.isValid():
            return
        if not self._detail_mode:
            self._detail_mode = True
            self.detail_container.show()
            self.view.set_column_count(1)
            QTimer.singleShot(0, self._resize_splitter)
            # 无论首次还是再次，都要把 index 抛给 presenter 填数据
        self.card_clicked.emit(index)

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex):
        # clearSelection() 会切到 invalid index —— 过滤掉，不误触
        if current.isValid():
            self._on_card_clicked(current)

    def _resize_splitter(self):
        w = max(self.splitter.width(), 500)
        card_w = max(int(w * 0.35), 200)
        self.splitter.setSizes([card_w, w - card_w])

    def _on_close_detail(self):
        if self._detail_mode:
            self._detail_mode = False
            self.view.set_column_count(2)
            self.detail_container.hide()
            self.view.clearSelection()

    def open_card(self, index: QModelIndex) -> None:
        """供 presenter 主动进入详情模式用（例如新建草稿后）。"""
        self._on_card_clicked(index)

    def _on_view_context_menu_requested(self, pos):
        """卡片区域右键：把 index 和全局坐标交给 presenter。"""
        index = self.view.indexAt(pos)
        if not index.isValid():
            return  # 空白处右键不弹
        global_pos = self.view.viewport().mapToGlobal(pos)
        self.context_menu_requested.emit(index, global_pos)


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = QWidget()
    widget = IngestSource(window)

    layout = QVBoxLayout(window)
    layout.addWidget(widget)
    window.resize(1000, 700)
    window.show()
    sys.exit(app.exec())
