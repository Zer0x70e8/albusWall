#
"""相册页：整页模糊背景 + 可滚动的内容区。

版式（自上而下）::

    close_button    关闭整页
    hover           悬浮操作行（编辑 / 新建相册）
    album_view      相册网格：全部相册（顶部）
    trash_view      回收站：虚拟 Trash 相册（底部，单行列表）

纯视图组件：不加载数据、不认识 model 的具体内容。外部用
:meth:`Album.set_album_model`（顶部相册网格）与 :meth:`Album.set_trash_model`
（底部回收站）直接注入模型即可，委托缺省时自动装配：顶部是正方形的
:class:`~albuswall.ui.delegate.AlbumCoverDelegate`，底部是单行排版的
:class:`~albuswall.ui.delegate.TrashAlbumDelegate`（左图标 / 弹簧 / 右名称）。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..delegate import AlbumCoverDelegate, TrashAlbumDelegate
from ..i18n import Retranslatable
from ..widgets.blur_overlay_label import BlurLabel
from ..widgets.square_grid import SquareGridView


from .utils import install_close_button

__all__ = ["Album"]

#: 底部回收站列表的上下留白（像素）：与网格之间 / 列表底部各留一点。
_TRASH_VIEW_MARGIN = 6
#: 外部注入了非 :class:`~albuswall.ui.delegate.TrashAlbumDelegate` 的委托时，
#: 用它猜行高（这种情况下委托自己负责排版，视图只保证「一行放得下」）。
_FALLBACK_TRASH_ROW_HEIGHT = 40


class Album(Retranslatable, BlurLabel):
    """相册页容器。"""

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout
    close_button: QPushButton

    root: QWidget
    root_layout: QVBoxLayout
    scroll_area: QScrollArea
    content: QWidget
    content_layout: QVBoxLayout

    hover: QFrame
    hover_layout: QVBoxLayout
    option_layout: QHBoxLayout
    edit_button: QPushButton
    add_button: QPushButton
    #: 顶部：相册网格
    album_view: SquareGridView
    #: 底部：回收站（只放虚拟 Trash 相册，单行列表）
    trash_view: QListView

    def __init__(self,
                 target: QWidget | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent, target=target, draw_label_content=False)

        # 默认委托（set_album_model / set_trash_model 首次调用时装配）
        self._album_delegate = None
        self._trash_delegate = None
        #: 已连接的回收站模型（换模型时要断开它的行数信号）
        self._trash_model = None

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)

        self.close_button = install_close_button(self)
        self.close_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.close_button.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True)

        self.root = QWidget(self)
        self.root_layout = QVBoxLayout(self.root)
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)

        self.content_layout.setSpacing(0)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)
        self.root_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea(self.root)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)  # 去掉自带的边框
        self.scroll_area.viewport().setAutoFillBackground(False)
        # 滚动容器不是可操作控件：它没有焦点指示器，留在 Tab 链里就会变成
        # 「按 Tab / 方向键后焦点不见了」。落焦控件可见性由 Qt 负责。
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.viewport().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.setWidget(self.content)

        self.root_layout.addWidget(self.scroll_area)

        # ---- 悬浮操作行 + 网格 ----
        self.hover = QFrame(self.content)
        self.hover_layout = QVBoxLayout(self.hover)
        self.hover_layout.setSpacing(0)
        self.hover_layout.setContentsMargins(0, 0, 0, 0)

        self.option_layout = QHBoxLayout()

        self.edit_button = QPushButton("", self.hover)
        self.add_button = QPushButton("+", self.hover)

        self.option_layout.addStretch()
        self.option_layout.addWidget(self.edit_button)
        self.option_layout.addWidget(self.add_button)

        self.album_view = SquareGridView(parent=self.content)

        self.hover_layout.addLayout(self.option_layout)
        self.hover_layout.addWidget(self.album_view)

        self.trash_view = QListView(self.content)

        self.content_layout.addWidget(self.hover)
        self.content_layout.addWidget(self.trash_view)
        self.content_layout.addStretch()

        self.main_layout.addWidget(self.close_button)
        self.main_layout.addWidget(self.root)

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用文案。"""
        self.edit_button.setText(self.tr("Edit"))

    # ------------------------------------------------------------------ #
    # 数据接口（直接接收模型）
    # ------------------------------------------------------------------ #
    def set_album_model(self, model, delegate=None) -> None:
        """设置**顶部相册网格**（``album_view``）的模型。

        :param model: 任意实现 ``ItemRole`` 的模型，通常是
            :class:`~albuswall.ui.model.AlbumModel`。
        :param delegate: 委托；``None`` 时按需装一个默认的
            :class:`~albuswall.ui.delegate.AlbumCoverDelegate`。
        """
        if delegate is None:
            delegate = self._default_album_delegate()
        self.album_view.bind_model(model, delegate)

    def set_trash_model(self, model, delegate=None) -> None:
        """设置**底部回收站**（``trash_view``）的模型。

        通常只有一行（虚拟 Trash 相册），所以按**单行列表**排版：
        「左图标 / 中间弹簧 / 右名称」，行高由委托的 ``sizeHint`` 决定；
        视图高度随行数自动收缩（空列表直接把视图藏起来）。

        :param model: 任意实现 ``ItemRole`` 的模型，通常是
            :class:`~albuswall.ui.model.AlbumModel`。
        :param delegate: 委托；``None`` 时按需装一个默认的
            :class:`~albuswall.ui.delegate.TrashAlbumDelegate`。
        """
        if delegate is None:
            delegate = self._default_trash_delegate()
        self._detach_trash_model()
        view = self.trash_view
        view.setModel(model)
        view.setItemDelegate(delegate)
        # 行高交给委托的 sizeHint，不设网格（网格是正方形格子用的）
        view.setGridSize(QSize())
        view.setViewMode(QListView.ViewMode.ListMode)
        view.setFlow(QListView.Flow.TopToBottom)
        view.setWrapping(False)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setUniformItemSizes(True)
        view.setMovement(QListView.Movement.Static)
        view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        view.setSelectionRectVisible(False)
        view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._trash_model = model
        self._connect_trash_model(model)
        self._sync_trash_height()

    def album_delegate(self):
        """当前相册网格的委托（可能是外部注入的）。"""
        return self.album_view.itemDelegate()

    def trash_delegate(self):
        """当前回收站列表的委托（可能是外部注入的）。"""
        return self.trash_view.itemDelegate()

    def refresh_trash_height(self) -> None:
        """按回收站当前行数重算视图高度（委托换行高后也可手动调）。"""
        self._sync_trash_height()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _default_album_delegate(self):
        """复用已装的默认相册委托，没有就新建一个。"""
        current = self.album_view.itemDelegate()
        if isinstance(current, AlbumCoverDelegate):
            return current
        delegate = AlbumCoverDelegate(self.album_view)
        self._album_delegate = delegate
        return delegate

    def _default_trash_delegate(self):
        """回收站用单行委托（左图标 / 弹簧 / 右名称），没有就新建一个。"""
        current = self.trash_view.itemDelegate()
        if isinstance(current, TrashAlbumDelegate):
            return current
        delegate = TrashAlbumDelegate(self.trash_view)
        self._trash_delegate = delegate
        return delegate

    # ---- 回收站行数 → 视图高度 ----
    def _trash_row_height(self) -> int:
        """单行高度：委托说了算，换委托也不会把视图高度算错。"""
        delegate = self.trash_view.itemDelegate()
        if isinstance(delegate, TrashAlbumDelegate):
            return delegate.row_height()
        # 外部委托：向它要一次 sizeHint（模型空的时候没有索引可问）
        model = self.trash_view.model()
        if model is not None and model.rowCount() > 0:
            option = QStyleOptionViewItem()
            option.widget = self.trash_view
            option.font = self.trash_view.font()
            height = delegate.sizeHint(option, model.index(0, 0)).height()
            if height > 0:
                return height
        return _FALLBACK_TRASH_ROW_HEIGHT

    def _sync_trash_height(self) -> None:
        """按行数把回收站视图钉成「刚好放下所有行」的高度。"""
        view = self.trash_view
        model = view.model()
        rows = model.rowCount() if model is not None else 0
        if rows <= 0:
            view.setFixedHeight(0)
            view.setVisible(False)
            return
        view.setVisible(True)
        view.setFixedHeight(rows * self._trash_row_height()
                              + 2 * _TRASH_VIEW_MARGIN)

    def _connect_trash_model(self, model) -> None:
        """行数变化时重算视图高度（模型可以是任意 QAbstractItemModel）。"""
        if model is None:
            return
        for name in (
                "rowsInserted",
                "rowsRemoved",
                "modelReset",
                "layoutChanged",
        ):
            signal = getattr(model, name, None)
            if signal is None:
                continue
            try:
                signal.connect(self._on_trash_rows_changed)
            except (RuntimeError, TypeError):  # pragma: no cover - 防御式
                continue

    def _detach_trash_model(self) -> None:
        """断开旧模型的行数信号（避免换模型后仍被旧模型推动重算）。"""
        model = self._trash_model
        self._trash_model = None
        if model is None:
            return
        for name in (
                "rowsInserted",
                "rowsRemoved",
                "modelReset",
                "layoutChanged",
        ):
            signal = getattr(model, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect(self._on_trash_rows_changed)
            except (RuntimeError, TypeError):
                continue

    def _on_trash_rows_changed(self, *_args) -> None:
        self._sync_trash_height()
