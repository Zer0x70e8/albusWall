#
""""""

from PySide6.QtCore import Qt, Signal, QModelIndex
from PySide6.QtWidgets import (
    QHBoxLayout, QSizePolicy, QWidget,
    QPushButton, QScrollArea, QSplitter, QVBoxLayout
)

from .detail_widget import IngestSourceDetailWidget
from ...widgets import BlurLabel, ColumnListView
from ...utils.qt_objectname_utils import auto_set_object_names


class IngestSource(BlurLabel):
    # 点击卡片后向外抛出 QModelIndex
    card_clicked = Signal(QModelIndex)

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

        self.setObjectName("IngestSource")

        self._setup_ui()

        auto_set_object_names(
            self,
            class_name_source=self,
            separator="",
            overwrite=True,
            camel_case=True
        )

    # ------------------------------------------------------------------ UI
    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        self.close_button = QPushButton("\u2715", self)
        self.apply_button = QPushButton(self.tr("apply"), self)
        self.add_button = QPushButton(self.tr("add"), self)

        self.content_layout = QHBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        # ---- splitter：左卡片 / 右详情 ----
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setStretchFactor(0, 1)  # 卡片视图占据主要伸缩空间
        self.splitter.setStretchFactor(1, 0)  # 详情容器保持固定宽度

        # ---- 左侧：卡片视图 ----
        self.view = ColumnListView(self.splitter)
        self.view.set_column_count(2)
        self.view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.view.clicked.connect(self._on_card_clicked)

        # ---- 右侧：详情容器（初始隐藏） ----
        self.detail_container = QWidget(self.splitter)
        self.detail_container.setMinimumWidth(200)
        self.detail_container.hide()

        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(0)

        # 关闭详情按钮（右上角）
        self.close_detail_btn_layout = QHBoxLayout()
        self.close_detail_btn_layout.setContentsMargins(8, 8, 8, 0)

        self.close_detail_btn = QPushButton("\u2715")
        self.close_detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_detail_btn.clicked.connect(self._on_close_detail)

        self.close_detail_btn_layout.addStretch(1)
        self.close_detail_btn_layout.addWidget(self.close_detail_btn)

        # 详情 widget 容器（内部使用滚动区）
        self.detail_widget_container = QWidget()
        self.detail_widget = IngestSourceDetailWidget(self.detail_widget_container)
        self.detail_widget_container_layout = QVBoxLayout(self.detail_widget_container)
        self.detail_widget_container_layout.addWidget(self.detail_widget)
        self.detail_widget_container_layout.addStretch()

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(self.detail_widget_container)

        self.detail_layout.addLayout(self.close_detail_btn_layout)
        self.detail_layout.addWidget(self.apply_button)
        self.detail_layout.addWidget(self.scroll_area, 1)

        # ---- 组装 splitter ----
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.detail_container)
        self.splitter.setSizes([800, 360])  # 初始宽度比例

        self.content_layout.addWidget(self.splitter)

        # ---- 顶部添加按钮 ----
        self.add_button_layout = QHBoxLayout()
        self.add_button_layout.addStretch()
        self.add_button_layout.addWidget(self.add_button)

        # ---- 顶层布局 ----
        self.main_layout.addWidget(self.close_button)
        self.main_layout.addLayout(self.add_button_layout)
        self.main_layout.addLayout(self.content_layout)

    # -------------------------------------------------------------- 数据接口
    def set_model(self, model, delegate):
        """设置数据模型和委托，供外部调用"""
        self.model = model
        self.delegate = delegate
        self.view.setModel(model)
        self.view.setItemDelegate(delegate)

    # -------------------------------------------------------------- 交互
    # noinspection PyUnusedLocal
    def _on_card_clicked(self, index: QModelIndex):
        # 首次点击卡片时进入"详情模式"
        if not self._detail_mode:
            self._detail_mode = True
            self.view.set_column_count(1)
            self.detail_container.show()

            total_width = max(self.width(), 500)
            card_width = int(total_width * 0.3)
            detail_width = total_width - card_width
            self.splitter.setSizes([card_width, detail_width])

        self.card_clicked.emit(index)

    def _on_close_detail(self):
        if self._detail_mode:
            self._detail_mode = False
            self.view.set_column_count(2)
            self.detail_container.hide()
            self.view.clearSelection()


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
