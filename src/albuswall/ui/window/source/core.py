#
"""导入源页：左侧卡片列表 + 右侧可滑出的表单，中间用 splitter 分隔。

纯视图组件：model / delegate 由外部注入（:meth:`IngestSource.set_model`），
卡片的右键菜单与点击只发意图信号。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QPoint,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...delegate import SourceCardDelegate
from ...i18n import Retranslatable
from ...model import SourceCardModel
from ...widgets import BlurLabel, ColumnListView
from ..utils import install_close_button
from .detail_widget import IngestSourceDetailWidget

__all__ = ["IngestSource"]


class IngestSource(Retranslatable, BlurLabel):
    """导入源页容器。"""

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    context_menu_requested = Signal(QModelIndex, QPoint)  # 卡片右键 (索引, 全局坐标)
    card_clicked = Signal(QModelIndex)                    # 点击 / 键盘激活卡片
    detail_closed = Signal()                              # 右侧表单被收起
    close_requested = Signal()                            # 关闭整页

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout
    close_button: QPushButton

    add_button_layout: QHBoxLayout
    add_button: QPushButton

    content_layout: QHBoxLayout
    splitter: QSplitter
    view: ColumnListView

    detail_container: QWidget
    detail_layout: QVBoxLayout
    close_detail_btn: QPushButton
    apply_button: QPushButton
    scroll_area: QScrollArea
    detail_widget_container: QWidget
    detail_widget_container_layout: QVBoxLayout
    detail_widget: IngestSourceDetailWidget

    # ---- 数据侧（由 set_model 注入，本组件不解释其内容） ----
    model: QAbstractItemModel | None
    delegate: QAbstractItemDelegate | None

    def __init__(self,
                 parent: QWidget | None = None,
                 target: QWidget | None = None):
        super().__init__(parent, target=target, draw_label_content=False)

        self.model = None
        self.delegate = None
        self._detail_mode = False

        self.setObjectName("IngestSource")

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)

        # ---- 按钮 ----
        self.close_button = install_close_button(self)
        self.close_button.clicked.connect(self.close_requested.emit)

        self.apply_button = QPushButton("")
        self.add_button = QPushButton("")

        # ---- 左右分栏 ----
        self.content_layout = QHBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)

        # ---- 左侧卡片列表 ----
        self.view = ColumnListView()
        self.view.set_column_count(2)
        self.view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(
            self._on_view_context_menu_requested)
        self.view.clicked.connect(self._on_card_clicked)
        # 键盘 Enter / Return：与点击同一条出口（方向键只移动高亮，不打开发表单）
        self.view.activated.connect(self._on_card_clicked)
        self.view.installEventFilter(self)

        self.splitter.addWidget(self.view)

        # ---- 右侧详情 ----
        self.detail_container = QWidget()
        self.detail_container.setMinimumWidth(200)
        self.detail_container.hide()

        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(0)

        self.close_detail_btn = install_close_button(self.detail_container)
        self.close_detail_btn.clicked.connect(self._on_close_detail)

        self.detail_widget_container = QWidget()
        self.detail_widget = IngestSourceDetailWidget()
        self.detail_widget_container_layout = QVBoxLayout(
            self.detail_widget_container)
        self.detail_widget_container_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_widget_container_layout.addWidget(self.detail_widget)
        self.detail_widget_container_layout.addStretch()

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(self.detail_widget_container)

        self.detail_layout.addWidget(self.close_detail_btn)
        self.detail_layout.addWidget(self.apply_button)
        self.detail_layout.addWidget(self.scroll_area, 1)

        self.splitter.addWidget(self.detail_container)
        self.splitter.setSizes([800, 360])

        self.content_layout.addWidget(self.splitter)

        # ---- 顶部 add 按钮 ----
        self.add_button_layout = QHBoxLayout()
        self.add_button_layout.setContentsMargins(0, 0, 0, 0)
        self.add_button_layout.setSpacing(0)
        self.add_button_layout.addStretch()
        self.add_button_layout.addWidget(self.add_button)

        # ---- 组装 ----
        self.main_layout.addWidget(self.close_button)
        self.main_layout.addLayout(self.add_button_layout)
        self.main_layout.addLayout(self.content_layout, 1)

        self._setup_tab_order()

    def _setup_tab_order(self) -> None:
        """详情页的键盘遍历顺序：关闭 → 应用 → 表单（阅读顺序）。

        两个坑都在这里一并堵上：

        * ``QScrollArea`` 默认是 Tab 停点，但它**没有焦点指示器** —— 从
          「应用」按方向键 / Tab 走到它上面，表现就是「焦点不见了」。它只是
          滚动容器，不是可操作控件，退出焦点链；滚到落焦控件由 Qt
          （``ensureWidgetVisible``）负责；
        * 控件创建顺序 ≠ 视觉阅读顺序（可勾选的触发分组建得早、排得靠下），
          所以表单内部顺序由 :meth:`IngestSourceDetailWidget.tab_order`
          显式给出，再和上面的两个按钮串成一条链。
        """
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.viewport().setFocusPolicy(Qt.FocusPolicy.NoFocus)

        chain = [
            self.close_detail_btn,
            self.apply_button,
            *self.detail_widget.tab_order(),
        ]
        for before, after in zip(chain, chain[1:], strict=False):
            QWidget.setTabOrder(before, after)

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用按钮文案。"""
        self.apply_button.setText(self.tr("Apply"))
        self.add_button.setText(self.tr("Add"))

    # ------------------------------------------------------------------ #
    # 数据接口
    # ------------------------------------------------------------------ #
    def set_model(self, model, delegate=None) -> None:
        """注入卡片列表的模型（委托可省，缺省装默认卡片委托）。

        :param model: 任意实现 :class:`~albuswall.ui.model.SourceCardRole`
            的模型，通常是 :class:`~albuswall.ui.model.SourceCardModel`。
        :param delegate: 委托；``None`` 时用
            :class:`~albuswall.ui.delegate.SourceCardDelegate`。
        """
        if delegate is None:
            delegate = self._default_delegate()
        self.model = model
        self.delegate = delegate
        self.view.bind_model(model, delegate)

    def set_source_cards(self, cards) -> None:
        """便捷入口：直接喂一批导入源对象（DTO / ``SourceCard`` 都行）。

        内部会新建 :class:`~albuswall.ui.model.SourceCardModel` 并装好默认
        委托；已经自己维护模型时请继续用 :meth:`set_model`。
        """
        model = self.model
        if not isinstance(model, SourceCardModel):
            model = SourceCardModel(parent=self)
            self.set_model(model)
        model.set_cards(cards)

    def cards_model(self) -> SourceCardModel | None:
        """当前卡片模型（外部注入的模型不是 ``SourceCardModel`` 时为 ``None``）。"""
        return self.model if isinstance(self.model, SourceCardModel) else None

    def _default_delegate(self):
        """复用已装的默认委托，没有就新建一个。"""
        if isinstance(self.delegate, SourceCardDelegate):
            return self.delegate
        return SourceCardDelegate(self.view)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    # noinspection PyUnusedLocal
    def _on_card_clicked(self, index: QModelIndex) -> None:
        # 首次点击卡片时进入「详情模式」
        if not index.isValid():
            return
        if not self._detail_mode:
            self._detail_mode = True
            self.detail_container.show()
            self.view.set_column_count(1)
            QTimer.singleShot(0, self._resize_splitter)
        # 无论首次还是再次，都要把 index 抛给外部填数据
        self.card_clicked.emit(index)

    def _resize_splitter(self) -> None:
        width = max(self.splitter.width(), 500)
        card_width = max(int(width * 0.35), 200)
        self.splitter.setSizes([card_width, width - card_width])

    def _on_close_detail(self) -> None:
        if self._detail_mode:
            self._detail_mode = False
            self.view.set_column_count(2)
            self.detail_container.hide()
            self.view.clearSelection()
            self.detail_closed.emit()

    def open_card(self, index: QModelIndex) -> None:
        """外部主动进入详情模式用（例如新建草稿后）。"""
        self._on_card_clicked(index)

    def _on_view_context_menu_requested(self, pos: QPoint) -> None:
        """卡片区域右键：把 index 和全局坐标交给外部。"""
        index = self.view.indexAt(pos)
        if not index.isValid():
            return  # 空白处右键不弹
        global_pos = self.view.viewport().mapToGlobal(pos)
        self.context_menu_requested.emit(index, global_pos)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        """键盘上下文菜单：Menu 键 / Shift+F10（焦点卡片处弹出）。"""
        if obj is self.view and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_Menu or (
                    key == Qt.Key.Key_F10
                    and mods & Qt.KeyboardModifier.ShiftModifier):
                index = self.view.currentIndex()
                if index.isValid():
                    rect = self.view.visualRect(index)
                    pos = (rect.center() if not rect.isEmpty()
                           else self.view.viewport().rect().center())
                else:
                    pos = self.view.viewport().rect().center()
                self._on_view_context_menu_requested(pos)
                event.accept()
                return True
        return super().eventFilter(obj, event)


if __name__ == "__main__":
    def _main() -> int:  # pragma: no cover - 手动预览入口
        import sys

        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)

        window = QWidget()
        widget = IngestSource(window)

        layout = QVBoxLayout(window)
        layout.addWidget(widget)
        window.resize(1000, 700)
        window.show()
        return app.exec()


    raise SystemExit(_main())
