#
""""""

from __future__ import annotations

from PySide6.QtCore import (
    Property, QItemSelectionModel, QModelIndex, QPoint, QSize, Qt, QTimer,
    Signal, QEvent
)
from PySide6.QtWidgets import (
    QAbstractItemView, QListView, QStyledItemDelegate, QFrame
)

DEFAULT_COLUMN_COUNT = 5
MIN_COLUMN_COUNT = 1
MAX_COLUMN_COUNT = 12
DEFAULT_CELL_SIZE = 128

#: 滚轮一格（``angleDelta`` 120）对应的滚动距离（像素）。
WHEEL_PIXEL_STEP = 30

#: 顶部允许的最大「过滚」距离（像素）；0 = 关掉顶部过滚。
OVERSCROLL_TOP_MAX = 128

#: 底部允许的最大「过滚」距离（像素）；0 = 关掉底部过滚。
OVERSCROLL_BOTTOM_MAX = 256

#: 缩放手势的间隔上限（毫秒）：超过这个时间没有继续缩放，锚点就作废。
ZOOM_ANCHOR_TIMEOUT_MS = 600

#: 缩放一档所需的滚轮量（``120`` = 一格标准滚轮）。
#:
#: 触控板（尤其是 Linux 的 libinput / macOS 的 ``pixelDelta``）一次只给几
#: 个像素的 ``delta``，逐事件换算「一个事件 = 一档」会让列数乱跳。所以这里
#: 把细碎的滚动量**攒够一档**才动一次列数（攒不满就什么也不做）—— 这就是
#: 缩放的防抖。
ZOOM_WHEEL_STEP = 120


class _ForcedSizeDelegate(QStyledItemDelegate):
    """内部 wrapper：强制 item 尺寸 = view.cell_size，忽略 inner 的 sizeHint。"""

    def __init__(self, inner, view: "TileView") -> None:
        super().__init__(view)
        self._inner = inner
        self._view = view

    def sizeHint(self, option, index) -> QSize:
        c = max(1, self._view.cell_size)
        return QSize(c, c)

    def paint(self, painter, option, index) -> None:
        if self._inner is not None:
            self._inner.paint(painter, option, index)
        else:
            super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index) -> bool:
        if self._inner is not None:
            return bool(self._inner.editorEvent(event, model, option, index))
        return super().editorEvent(event, model, option, index)

    def createEditor(self, parent, option, index):
        if self._inner is not None:
            return self._inner.createEditor(parent, option, index)
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index) -> None:
        if self._inner is not None:
            self._inner.setEditorData(editor, index)
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index) -> None:
        if self._inner is not None:
            self._inner.setModelData(editor, model, index)
        else:
            super().setModelData(editor, model, index)

    def updateEditorGeometry(self, editor, option, index) -> None:
        if self._inner is not None:
            self._inner.updateEditorGeometry(editor, option, index)
        else:
            super().updateEditorGeometry(editor, option, index)

    def destroyEditor(self, editor, index) -> None:
        if self._inner is not None:
            self._inner.destroyEditor(editor, index)
        else:
            super().destroyEditor(editor, index)


class TileView(QListView):
    """基于 QListView (IconMode) 的等大方格虚拟滚动视图。

    这是项目里**唯一**的缩略图网格控件（内容层 ``Content`` 就是它的子类）：

    * 列数是唯一真值，格子边长 = ``(viewport_width - 1) // 列数`` 派生；
    * 所有格子等大方框，委托只管 ``paint``（尺寸由 :class:`_ForcedSizeDelegate`
      强制，委托写 ``sizeHint`` 也不会生效）；
    * 数据来自 Qt 模型（行号 = 全局索引），缩略图走 ``DecorationRole`` /
      ``ItemRole.Pixmap``，异步回填由调用方 ``set_pixmap`` 到模型上。

    信号语义（调用方按「行号」= 全局索引消费，与列表模型一一对应）：

    ``unit_pressed`` / ``unit_clicked`` / ``unit_right_clicked`` /
    ``unit_activated`` / ``unit_previewed`` 的参数都是**行号**（全局索引）；
    ``cursor_changed`` 同理（``-1`` = 没有光标）。

    键盘：方向键 / Home / End / PageUp / PageDown 由 ``QListView`` 原生处理，
    回车 = 激活（详情），空格 = 快速预览 —— 两个键在这里是**分开**的。
    因为本控件是 ``QAbstractItemView``，应用级
    :class:`~albuswall.ui.utils.activation_keys.ActivationKeyFilter` 会把空格
    改写成 ``activated``；视图用类属性 :attr:`space_previews` 声明「空格由我
    自己处理」，过滤器看到就不改写（见 ``activation_keys._filter_item_view``）。

    过滚（overscroll）
    ==================

    滚到顶部还能继续往上滚一点、滚到底部还能继续往下滚一点：内容整体被推开
    ``最多 128px（顶）/ 256px（底）``，露出后面那层（透明背景 → 模糊壁纸）。
    **不回弹**：偏移停在那里，直到用户往反方向滚才收回去（与旧实现的
    ``overscroll_top`` / ``bottom_overscroll`` 语义一致）。

    实现上不是改滚动条范围（``QAbstractSlider`` 会夹紧），而是给滚动条值再加
    一个 ``_overscroll`` 偏移；真正让内容位移的是移动 viewport 本身
    （``_apply_overscroll``）。滚轮先吃掉过滚偏移，再动滚动条；两边都到底了
    （偏移也用完）就把滚轮事件让给父层（``event.ignore()``）。

    缩放（``Ctrl`` + 滚轮 / ``set_column_count``）
    ============================================

    列数 ±1 即缩放一档，格子尺寸随之重算。位置锚在**一格**上（见
    :meth:`set_column_count`），放大再缩小能原地返回。

    滚轮量是**攒着用**的：触控板一次只给几个像素的 ``delta``，攒够一档
    （:data:`ZOOM_WHEEL_STEP` = 一格标准滚轮；``pixelDelta`` 按 30px ≈ 一格
    换算）才缩一次 —— 这就是缩放的防抖，不会一碰触控板列数就乱跳。

    位置稳定性
    ==========

    用户滚到哪儿、过滚到哪儿，**刷新与选中都不许动它**：

    * 选中 / 挪光标到已在视野内的格子：位置完全不变（真的要滚过去才滚）；
    * 换数据、过滤、换主题、语言切换、缩略图回填（``dataChanged``）、尺寸
      变化：位置（含过滚偏移）原样保留，只有内容真的变短时才被滚动条夹紧。

    这条契约要绕开 Qt 的一个固有行为：``QListView`` **跟着 current 走** ——
    把当前格设到视野外的行时，视图会把那一行滚进视野。模型 reset 后光标失效、
    被重新落到第 0 行，于是「刷新一次就弹回顶部」。Qt 没有「只设 current、
    不滚动」的公开接口（滚动在 Qt 内部完成，与调不调 ``setCurrentIndex``
    无关，连直接改 selectionModel 的 current 也会滚），所以
    :meth:`_ensure_cursor` 的做法是：记下当前总位置，设完光标再原样放回去。
    """

    #: 空格 = 「快速预览」而不是「激活」：应用级激活键过滤器读到这个标记
    #: 就不再把手里的空格改写成 ``activated``（回到 Qt 的默认语义）。
    space_previews = True

    scroll_changed = Signal(int)
    visible_range_changed = Signal(int, int, int)
    cursor_changed = Signal(int)
    column_count_changed = Signal(int)

    unit_pressed = Signal(int)
    unit_clicked = Signal(int)
    unit_right_clicked = Signal(int)
    unit_activated = Signal(int)
    unit_previewed = Signal(int)

    def __init__(
            self,
            parent=None,
            *,
            column_count: int = DEFAULT_COLUMN_COUNT,
    ) -> None:
        super().__init__(parent)
        self._columns = self._clamp_columns(column_count)
        self._cell_size = DEFAULT_CELL_SIZE
        self._request_counter = 0
        self._last_start = -1
        self._last_end = -1
        # 模型结构变化时光标要挪到合法位置：挪的过程中不要顺手滚动视口
        # （否则刷新一次列表就把用户滚到的位置弹回顶部）。
        self._restoring_cursor = False

        # 过滚：滚动条值之外的额外位移（负 = 越过顶部，正 = 越过底部）。
        self.overscroll_top = OVERSCROLL_TOP_MAX
        self._bottom_overscroll = OVERSCROLL_BOTTOM_MAX
        self._overscroll = 0
        self._wheel_pixel_step = WHEEL_PIXEL_STEP
        # 正在由 _set_scroll_position 驱动滚动条：此时 valueChanged 不算「外部改动」
        self._setting_position = False

        # 缩放手势锚点：``(全局索引, 目标视口 y)``。连续缩放（Ctrl + 滚轮连滚）
        # 一直把**同一格**按在同一个位置上，来回缩放才能原地返回、不漂。
        self._zoom_anchor: tuple[int, int] | None = None
        # 攒着的滚轮量（防抖：触控板一次只给一点点，攒够 ZOOM_WHEEL_STEP 才缩一档）
        self._zoom_delta = 0
        # viewport 被 Qt 摆回原位后需要一个「下一轮」再落位（见 eventFilter）
        self._reapply_pending = False
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(ZOOM_ANCHOR_TIMEOUT_MS)
        self._zoom_timer.timeout.connect(self._clear_zoom_anchor)

        # 网格自己消费方向键 / Home / End / 回车 / 空格：外部若接入全局
        # 快捷键处理器，看到这个属性就不应再抢走无修饰键。
        self.setProperty("keymap_passthrough", True)

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        # 消除系统默认的 1px 边框，防止因边框误差导致换行计算错误
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # 强制尺寸的 wrapper delegate
        self._inner_delegate = None
        self._forced_delegate = _ForcedSizeDelegate(None, self)
        super().setItemDelegate(self._forced_delegate)

        # 监控 viewport 尺寸变化，处理滚动条显隐引起的宽度变化
        self.viewport().installEventFilter(self)

        self._wire_signals()
        self._recalc_grid()

    def updateGeometries(self) -> None:
        """``QAbstractItemView`` 每次重排滚动条 / viewport 之后补一次过滚偏移。

        Qt 在尺寸、范围、布局变化后会重新摆放 viewport（把它摆回 ``(0, 0)``），
        我们挪出去的那点过滚位移就没了 —— 这里补回来，免得「缩放 / 刷新后
        边缘位移自己弹回去」。
        """
        super().updateGeometries()
        if getattr(self, "_overscroll", 0):
            self._apply_overscroll()

    def _overscroll_target(self) -> QPoint:
        """过滚态下 viewport 应该待的位置。"""
        base = self.contentsRect()
        return QPoint(base.x(), base.y() - self._overscroll)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.viewport():
            if event.type() == QEvent.Type.Resize:
                self._recalc_grid()
                self._after_geometry_changed()
            elif event.type() == QEvent.Type.Move and self._overscroll:
                # Qt 在重排布局时会直接把 viewport 摆回 (0, 0)（不走
                # updateGeometries() 这条虚函数），过滚位移被悄悄抹掉 ——
                # 发现就排一次补落位。
                if self.viewport().pos() != self._overscroll_target():
                    self._schedule_overscroll_reapply()
        return super().eventFilter(obj, event)

    def _schedule_overscroll_reapply(self) -> None:
        if self._reapply_pending:
            return
        self._reapply_pending = True
        QTimer.singleShot(0, self, self._reapply_overscroll)

    def _reapply_overscroll(self) -> None:
        self._reapply_pending = False
        if self._overscroll:
            self._apply_overscroll()

    # 强制尺寸：delegate 包装 
    def setItemDelegate(self, delegate) -> None:
        self._inner_delegate = delegate
        self._forced_delegate = _ForcedSizeDelegate(delegate, self)
        super().setItemDelegate(self._forced_delegate)

    def itemDelegate(self):
        return self._inner_delegate

    # 信号连接 -
    def _wire_signals(self) -> None:
        self.pressed.connect(self._on_pressed)
        self.clicked.connect(self._on_clicked)
        self.activated.connect(self._on_activated)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value)

    def setModel(self, model) -> None:
        old = self.model()
        if old is not None:
            for sig in (
                    old.rowsInserted,
                    old.rowsRemoved,
                    old.modelReset,
                    old.layoutChanged,
            ):
                try:
                    sig.disconnect(self._on_model_structure_changed)
                except (RuntimeError, TypeError):
                    pass

        old_sel = self.selectionModel()
        if old_sel is not None:
            try:
                old_sel.currentChanged.disconnect(self._on_current_changed)
            except (RuntimeError, TypeError):
                pass

        super().setModel(model)

        sel = self.selectionModel()
        if sel is not None:
            sel.currentChanged.connect(self._on_current_changed)

        if model is not None:
            model.rowsInserted.connect(self._on_model_structure_changed)
            model.rowsRemoved.connect(self._on_model_structure_changed)
            model.modelReset.connect(self._on_model_structure_changed)
            model.layoutChanged.connect(self._on_model_structure_changed)

        self._last_start = -1
        self._last_end = -1
        self._ensure_cursor()
        self._after_geometry_changed()
        self._emit_visible_range_if_changed()
        # 行数变化后滚动条范围由 QListView 在布局时更新，下一轮事件循环
        # 再量一次，可见范围才不会沿用旧值。
        QTimer.singleShot(0, self, self.refresh_visible_range)

    # 槽 -------
    def _on_pressed(self, index: QModelIndex) -> None:
        if index.isValid():
            self.unit_pressed.emit(index.row())

    def _on_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self.unit_clicked.emit(index.row())

    def _on_activated(self, index: QModelIndex) -> None:
        if index.isValid():
            self.unit_activated.emit(index.row())

    def _on_context_menu(self, pos: QPoint) -> None:
        idx = self.indexAt(pos)
        if idx.isValid():
            self.unit_right_clicked.emit(idx.row())

    def _on_scroll_value(self, value: int) -> None:
        if not self._setting_position:
            self._clear_zoom_anchor()
        if not self._setting_position and self._overscroll:
            # 外部动了滚动条（拖滑块 / 键盘翻页 / scrollTo）：过滚立即失效，
            # 否则「滚动条值 + 过滚」会拼出一个不连续的位置。
            self._set_overscroll(0)
        self.scroll_changed.emit(self.scroll_y)
        self._emit_visible_range_if_changed()

    def _on_current_changed(
            self, current: QModelIndex, previous: QModelIndex
    ) -> None:
        self._clear_zoom_anchor()
        if current.isValid() and not self._restoring_cursor:
            self.scrollTo(
                current, QAbstractItemView.ScrollHint.EnsureVisible
            )
        self.cursor_changed.emit(
            current.row() if current.isValid() else -1
        )

    def _on_model_structure_changed(self, *args) -> None:
        self._clear_zoom_anchor()
        self._ensure_cursor()
        self._after_geometry_changed()
        self._last_start = -1
        self._last_end = -1
        self._emit_visible_range_if_changed()
        QTimer.singleShot(0, self, self.refresh_visible_range)

    def _ensure_cursor(self) -> None:
        """数据变了：把键盘光标收敛到合法格子（无数据 = 清空光标）。

        换相册 / 过滤 / 刷新都会重建模型：光标若还指着旧行号就会指向别的
        资产。这里**绝不碰滚动位置**（包括过滚偏移）—— 刷新不该把用户滚到
        的地方挪走。所以不走 ``QAbstractItemView.setCurrentIndex()``：那个
        接口内部会 ``scrollTo(index)``，模型重建后光标失效时一调就把视图拉回
        顶部；改用 ``selectionModel().setCurrentIndex(NoUpdate)``，只换光标。
        """
        model = self.model()
        total = model.rowCount() if model is not None else 0
        current = self.currentIndex()
        sel = self.selectionModel()
        if sel is None:
            return

        # 视图会「跟着 current 走」：把光标设到看不见的行时，Qt 内部会把
        # 该行滚进视野（模型重建后光标指向第 0 行 → 画面弹回顶部）。所以
        # 先记下位置，设完光标再原样放回去；列表空了就回顶端。
        saved = self.scroll_y

        self._restoring_cursor = True
        try:
            if total <= 0:
                if current.isValid():
                    sel.clearCurrentIndex()
            elif not current.isValid():
                sel.setCurrentIndex(
                    model.index(0, 0),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
            elif current.row() >= total:
                sel.setCurrentIndex(
                    model.index(total - 1, 0),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._restoring_cursor = False
            self._set_scroll_position(saved if total > 0 else 0)

    # 网格：唯一真值是列数，cell_size 派生 
    def _clamp_columns(self, n: int) -> int:
        return max(MIN_COLUMN_COUNT, min(MAX_COLUMN_COUNT, int(n)))

    def _layout_width(self) -> int:
        """算格子用的「整幅宽度」：把滚动条占的位置**常驻**预留出来。

        滚动条是 ``ScrollBarAsNeeded``：内容一多就出现，viewport 随之变窄。
        如果格子按变窄后的宽度算，格子变小 → 内容变矮 → 滚动条又消失 →
        格子变大 …… 会在「刚好需要 / 刚好不需要」的临界尺寸上反复抖动。
        所以统一按「滚动条常驻」的口径算：格子尺寸与滚动条显隐无关。
        """
        bar = self.verticalScrollBar()
        reserve = max(
            bar.sizeHint().width(),
            bar.width() if bar.isVisible() else 0,
        )
        return self.width() - reserve

    def _recalc_grid(self) -> None:
        w = self._layout_width()
        if w <= 0:
            return

        # 公式：cell = (w - 1) // columns
        # 解释：强制让 (cell * columns) < w，绝对避免因恰等于可用宽度
        # 或 1px 误差导致 QListView 判定换行（掉列）。
        # 右侧最大留白仅为 (w % columns) + 1 像素，视觉上完全无感。
        cell = max(1, (w - 1) // self._columns)

        if cell == self._cell_size and self.gridSize().width() == cell:
            return

        self._cell_size = cell
        new_size = QSize(cell, cell)
        if self.gridSize() != new_size:
            self.setGridSize(new_size)
            self.doItemsLayout()
        self._emit_visible_range_if_changed()

    def set_column_count(
            self, n: int, *, anchor_pos: QPoint | None = None,
    ) -> None:
        """缩放：改列数（格子尺寸随之重算）。

        位置按**一格**锚定：缩放前后那一刻保持「同一张图、同样的顶端偏移」，
        内容不跳也不回弹。

        * ``anchor_pos=None``（程序化调用）：锚 viewport 左上角那一格；
        * 给了 ``anchor_pos``（``Ctrl`` + 滚轮）：锚鼠标下那一格，围着指针缩放；
        * 同一次缩放手势（滚轮连滚）一直用**同一个**锚点（见 ``_zoom_anchor``），
          所以放大再缩小能原地返回、不累积漂移。

        旧实现按「行号」保位置：格子一变行高就变，内容整体上下跳一截。
        """
        n = self._clamp_columns(int(n))
        if n == self._columns:
            return

        anchor = self._zoom_anchor
        model = self.model()
        if anchor is not None and (
                model is None or not 0 <= anchor[0] < model.rowCount()):
            anchor = None
        if anchor is None:
            # 过滚时锚左上角（偏移 0）：指针锚定会换算出一个新的滚动值，
            # 顺手把边缘那点位移吃掉 —— 缩放不该改变「停在边缘」这件事。
            anchor = self._capture_anchor(
                None if self._overscroll else anchor_pos)
        self._columns = n
        self._recalc_grid()
        # QListView 的重新布局可能是「延迟执行」的：先把它做完，否则它会在
        # 我们落位之后再把滚动条挪一次（看起来就是缩放后位置回弹）。
        self.executeDelayedItemsLayout()
        self._restore_anchor(anchor)
        # 重新布局会把 viewport 摆回原位，过滚偏移要再落一次
        self._apply_overscroll()
        self.refresh_visible_range()
        # 记住这次的锚点：连滚缩放期间一直按同一格，不累积漂移
        self._zoom_anchor = anchor
        if anchor is not None:
            self._zoom_timer.start()
        self.column_count_changed.emit(n)

    def _clear_zoom_anchor(self) -> None:
        self._zoom_anchor = None
        self._zoom_delta = 0

    def _accumulate_zoom(self, delta: int, pos: QPoint | None = None) -> None:
        """``Ctrl`` + 滚轮缩放：把滚动量攒够一档（:data:`ZOOM_WHEEL_STEP`）再缩。

        防抖要点（触控板一次只给几个像素的 delta）：

        * 攒不满一档 → 什么都不做（列数不会抖）；
        * 攒够 → 按攒到的档数一次缩到位，余量留着（下一档接着用）；
        * 反向滚 → 先清掉上个方向的余量，免得「先攒了一半又倒回去」时卡住；
        * 已经到列数上限 / 下限 → 余量清零（不能攒着，否则反向时一次跳好几档）。
        """
        if self._zoom_delta and (delta > 0) != (self._zoom_delta > 0):
            self._zoom_delta = 0
        self._zoom_delta += int(delta)

        steps = int(self._zoom_delta / ZOOM_WHEEL_STEP)
        if steps == 0:
            return
        self._zoom_delta -= steps * ZOOM_WHEEL_STEP

        target = self._clamp_columns(self._columns - steps)
        if target == self._columns:
            self._zoom_delta = 0  # 到边了，别留余量
            return
        self.set_column_count(target, anchor_pos=pos)

    def _capture_anchor(
            self, pos: QPoint | None = None,
    ) -> tuple[int, int] | None:
        """记下某个位置的格子：``(全局索引, 它在视口里的 y)``。

        ``pos=None`` 时锚定 viewport 左上角那一格。位置**用算术推**（等大
        方格 + ``gridSize`` 就是布局本身），不问 ``visualRect()`` —— 重新布局
        前后那份矩形可能是过期的（滚动条值刚改、`QListView` 的布局还挂着）。
        """
        model = self.model()
        total = model.rowCount() if model is not None else 0
        cell = self._cell_size
        if total <= 0 or cell <= 0:
            return None

        point = QPoint(0, 0) if pos is None else QPoint(pos)
        x = max(0, min(point.x(), max(0, self.viewport().width() - 1)))
        y = max(0, min(point.y(), max(0, self.viewport().height() - 1)))

        cols = max(1, self._columns)
        top_y = max(0, self.verticalScrollBar().value())
        row = (top_y + y) // cell
        col = min(x // cell, cols - 1)
        index = row * cols + col
        if index >= total:
            index = total - 1
            row = index // cols
        return index, row * cell - top_y

    def _restore_anchor(self, anchor: tuple[int, int] | None) -> None:
        """把缩放前锚定的格子在视口里放回原来的纵向位置。"""
        if anchor is None:
            return
        index, offset = anchor
        model = self.model()
        total = model.rowCount() if model is not None else 0
        cell = self._cell_size
        if total <= 0 or cell <= 0:
            self._set_scroll_position(0)
            return
        index = max(0, min(int(index), total - 1))
        row = index // max(1, self._columns)
        target = row * cell - int(offset)
        # 过滚偏移照样保留：缩放不该顺手把边缘那点位移收掉
        self._set_scroll_position(target + self._overscroll)

    # qProperty 
    @Property(int, notify=column_count_changed)
    def column_count(self) -> int:
        return self._columns

    @column_count.setter
    def column_count(self, n: int) -> None:
        self.set_column_count(n)

    @Property(int, notify=scroll_changed)
    def scroll_y(self) -> int:
        """总滚动位置 = 滚动条值 + 过滚偏移（可能为负 / 超过最大值）。"""
        return self.verticalScrollBar().value() + self._overscroll

    @scroll_y.setter
    def scroll_y(self, y: int) -> None:
        self.set_scroll_y(y)

    def set_scroll_y(self, y: int) -> None:
        self._set_scroll_position(int(y))

    @property
    def overscroll(self) -> int:
        """当前过滚偏移（``<0`` 越过顶部 / ``>0`` 越过底部 / ``0`` 正常）。"""
        return self._overscroll

    @property
    def bottom_overscroll(self) -> int:
        return self._bottom_overscroll

    @bottom_overscroll.setter
    def bottom_overscroll(self, value: int) -> None:
        self._bottom_overscroll = max(0, int(value))
        self._set_scroll_position(self.scroll_y)

    @property
    def cell_size(self) -> int:
        return self._cell_size

    @property
    def scroll_row(self) -> int:
        if self._cell_size <= 0:
            return 0
        return max(0, self.scroll_y // self._cell_size)

    @scroll_row.setter
    def scroll_row(self, row: int) -> None:
        self.set_scroll_row(row)

    def set_scroll_row(self, row: int) -> None:
        row = max(0, int(row))
        self.set_scroll_y(row * self._cell_size)

    @property
    def max_item_index(self) -> int:
        m = self.model()
        return (m.rowCount() - 1) if m is not None else -1

    # 可见范围 -
    def _visible_range(self) -> tuple[int, int]:
        model = self.model()
        total = model.rowCount() if model is not None else 0
        if total <= 0 or self._cell_size <= 0:
            return (0, -1)
        bar = self.verticalScrollBar()
        top_y = bar.value()
        vh = self.viewport().height()
        total_rows = (total + self._columns - 1) // self._columns
        start_row = max(0, top_y // self._cell_size)
        end_row = max(0, (top_y + vh - 1) // self._cell_size)
        start_row = min(start_row, total_rows - 1)
        end_row = min(end_row, total_rows - 1)
        start_idx = min(start_row * self._columns, total - 1)
        end_idx = min((end_row + 1) * self._columns - 1, total - 1)
        return (start_idx, end_idx)

    def _emit_visible_range_if_changed(self) -> None:
        if not self.isVisible():
            return
        start, end = self._visible_range()
        if start == self._last_start and end == self._last_end:
            return
        self._last_start = start
        self._last_end = end
        self._request_counter += 1
        self.visible_range_changed.emit(start, end, self._request_counter)

    def refresh_visible_range(self) -> None:
        self._last_start = -1
        self._last_end = -1
        self._emit_visible_range_if_changed()

    # 过滚：滚动条值 + 偏移 -
    def _set_scroll_position(self, position: int) -> None:
        """设置「总滚动位置」（含过滚偏移），越界按过滚上限夹紧。

        ``position`` 的口径与 :attr:`scroll_y` 一致：``0`` 是内容顶端，
        ``-N`` 表示越过顶部 N 像素，``max + N`` 表示越过底部 N 像素。
        """
        bar = self.verticalScrollBar()
        lower = bar.minimum() - max(0, int(self.overscroll_top))
        upper = bar.maximum() + max(0, int(self._bottom_overscroll))
        position = max(lower, min(int(position), upper))

        value = max(bar.minimum(), min(position, bar.maximum()))
        overscroll = position - value

        if overscroll != self._overscroll:
            self._set_overscroll(overscroll)
            if bar.value() == value:
                # 滚动条值没动 → 不会有 valueChanged，这里补发
                self.scroll_changed.emit(self.scroll_y)
        if bar.value() != value:
            self._setting_position = True
            try:
                bar.setValue(value)  # → _on_scroll_value（scroll_changed / 可见范围）
            finally:
                self._setting_position = False

    def reset_overscroll(self) -> None:
        """清掉过滚偏移（键盘导航 / 换数据 / 改列数时收回内容）。"""
        if self._overscroll == 0:
            return
        self._set_scroll_position(self.verticalScrollBar().value())

    def _set_overscroll(self, value: int) -> None:
        value = int(value)
        if value == self._overscroll:
            return
        self._overscroll = value
        self._apply_overscroll()
        self.update()

    def _after_geometry_changed(self) -> None:
        """尺寸 / 范围变了：过滚偏移按新的滚动上限重新夹紧，并重新落位。

        ``QAbstractScrollArea`` 在 resize / 滚动条策略变化时会重排 viewport，
        我们挪出去的那一点位置会被抹掉 —— 这里补回来。
        """
        self._set_scroll_position(self.scroll_y)
        self._apply_overscroll()

    def _apply_overscroll(self) -> None:
        """把 viewport 整体挪开 ``_overscroll`` 像素，露出后面的背景。

        滚动条值被 ``QAbstractSlider`` 夹在 ``[0, maximum]``，所以真正的
        「多滚出去一点」只能靠移动 viewport 实现（它连同里面的格子一起位移，
        命中测试仍然是 viewport 局部坐标，鼠标不会错位）。
        """
        target = self._overscroll_target()
        if self.viewport().pos() != target:
            self.viewport().move(target)
        self.viewport().update()

    # 事件 -----
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._recalc_grid()
        self._after_geometry_changed()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._recalc_grid()
        self._after_geometry_changed()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            angle = event.angleDelta().y()
            pixels = event.pixelDelta().y()
            if angle:
                delta = angle
            elif pixels:
                # 触控板 / 高分滚动给的是像素量：按「30px ≈ 一格滚轮」换算，
                # 这样两指滑一小段才缩一档，而不是一碰就跳。
                delta = pixels * (ZOOM_WHEEL_STEP / max(1, WHEEL_PIXEL_STEP))
            else:
                event.ignore()
                return
            # 累积到一档才缩（触控板的小碎步不会把列数打乱）
            self._accumulate_zoom(delta, event.position().toPoint())
            event.accept()
            return

        self._zoom_delta = 0  # 一滚普通滚轮 = 缩放手势结束
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return

        # 一格滚轮（120）= 30px；先吃掉过滚偏移，再动滚动条。
        step = self._wheel_pixel_step * delta / 120.0
        before = self.scroll_y
        self._set_scroll_position(before - int(round(step)))
        if self.scroll_y == before:
            # 滚动条到底、过滚也用完了：把滚轮事件让给父层
            event.ignore()
            return
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            idx = self.currentIndex()
            if idx.isValid():
                self.unit_previewed.emit(idx.row())
                event.accept()
                return
        super().keyPressEvent(event)

    # 键盘光标 -
    def cursor_index(self) -> int:
        idx = self.currentIndex()
        return idx.row() if idx.isValid() else -1

    def set_cursor_index(self, row: int) -> None:
        model = self.model()
        total = model.rowCount() if model is not None else 0
        if total <= 0:
            sel = self.selectionModel()
            if sel is not None:
                sel.clearCurrentIndex()
            return
        row = max(0, min(int(row), total - 1))
        idx = model.index(row, 0)
        self.setCurrentIndex(idx)
        self.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)

    def move_cursor(self, delta: int) -> None:
        base = self.cursor_index()
        if base < 0:
            base = 0
        self.set_cursor_index(base + int(delta))

    def activate_cursor(self) -> bool:
        idx = self.currentIndex()
        if idx.isValid():
            self.unit_activated.emit(idx.row())
            return True
        return False

    def preview_cursor(self) -> bool:
        idx = self.currentIndex()
        if idx.isValid():
            self.unit_previewed.emit(idx.row())
            return True
        return False

    # 换模型：按行号恢复滚动位置 ---------------------------------------

    def swap_model(self, model) -> None:
        saved_row = self.scroll_row
        self.setModel(model)
        QTimer.singleShot(
            0, self, lambda: self._restore_scroll_row(saved_row))

    def _restore_scroll_row(self, row: int) -> None:
        bar = self.verticalScrollBar()
        target = max(0, int(row)) * self._cell_size
        bar.setValue(min(target, bar.maximum()))


if __name__ == "__main__":
    import sys

    from PySide6.QtCore import QAbstractListModel
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication, QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton,
        QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
    )


    # ============================================================
    # 1. Model：一行 = 一个 tile，DecorationRole 返回 QPixmap
    # ============================================================

    class DemoModel(QAbstractListModel):
        """演示用模型。实际项目里图应从 LRU 缓存取，miss 返回 None，
        再通过 visible_range_changed 异步补图。这里简化为即时生成纯色块。"""

        def __init__(self, count: int = 500, parent=None) -> None:
            super().__init__(parent)
            self._count = count
            self._cache: dict[int, QPixmap] = {}

        def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
            return 0 if parent.isValid() else self._count

        def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
            if not index.isValid():
                return None
            row = index.row()
            if role == Qt.ItemDataRole.DecorationRole:
                return self._pixmap(row)
            if role == Qt.ItemDataRole.DisplayRole:
                return f"Item {row}"
            if role == Qt.ItemDataRole.UserRole:
                return row
            return None

        def _pixmap(self, row: int) -> QPixmap:
            pm = self._cache.get(row)
            if pm is not None:
                return pm
            pm = QPixmap(128, 128)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            hue = (row * 37) % 360
            p.setBrush(QColor.fromHsv(hue, 150, 220))
            p.setPen(QPen(QColor(0, 0, 0, 60), 2))
            p.drawRoundedRect(2, 2, 124, 124, 12, 12)
            p.end()
            self._cache[row] = pm
            return pm


    # ============================================================
    # 2. Delegate：只 paint，不需要 sizeHint
    # ============================================================

    class DemoDelegate(QStyledItemDelegate):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._sel_pen = QPen(QColor(80, 160, 255), 3)

        def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
            painter.save()
            r = option.rect

            painter.fillRect(r, QColor(28, 28, 32))

            img_rect = r.adjusted(6, 6, -6, -6)
            pm = index.data(Qt.ItemDataRole.DecorationRole)
            if isinstance(pm, QPixmap) and not pm.isNull():
                painter.drawPixmap(img_rect, pm)

            painter.setPen(QColor(220, 220, 220))
            painter.setFont(QFont("Arial", 10))
            painter.drawText(
                r.adjusted(0, 0, 0, -4),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                str(index.row()),
            )

            if option.state & QStyle.StateFlag.State_Selected:
                painter.setPen(self._sel_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(r.adjusted(2, 2, -2, -2), 8, 8)

            painter.restore()

        # 不需要 sizeHint —— TileView 内部 wrapper 已强制成 cell_size × cell_size


    # ============================================================
    # 3. 主窗口：只有列数控制，尺寸随窗口自动
    # ============================================================

    class DemoWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(
                "TileView Demo — 只有列数控制，尺寸随窗口全自动"
            )
            self.resize(1100, 760)

            self.view = TileView(self, column_count=5)
            self.view.setItemDelegate(DemoDelegate(self.view))
            self.view.setModel(DemoModel(500, self))

            # 信号 ----
            self.view.visible_range_changed.connect(self._on_visible_range)
            self.view.column_count_changed.connect(
                lambda _n: self._refresh_labels()
            )
            self.view.unit_clicked.connect(
                lambda r: self.status(f"clicked {r}")
            )
            self.view.unit_activated.connect(
                lambda r: self.status(f"activated {r}")
            )
            self.view.unit_previewed.connect(
                lambda r: self.status(f"preview (Space) {r}")
            )
            self.view.unit_right_clicked.connect(self._on_right_click)

            # 顶部工具条：只有列数控制 ----
            bar = QWidget(self)
            row = QHBoxLayout(bar)
            row.setContentsMargins(8, 6, 8, 6)
            row.setSpacing(8)

            row.addWidget(QLabel("列数:"))
            row.addWidget(self._btn("−", lambda: self._bump_columns(-1)))
            row.addWidget(self._btn("+", lambda: self._bump_columns(+1)))

            self.lbl_columns = QLabel()
            row.addWidget(self.lbl_columns)

            row.addStretch(1)

            self.lbl_status = QLabel("就绪")
            row.addWidget(self.lbl_status)

            # 布局 ----
            central = QWidget(self)
            v = QVBoxLayout(central)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            v.addWidget(bar)
            v.addWidget(self.view, 1)
            self.setCentralWidget(central)

            # 窗口尚未 show 时 viewport 宽度可能为 0，延迟一次刷新。
            QTimer.singleShot(0, self._refresh_labels)

        # 工具 ----

        def _btn(self, text: str, cb) -> QPushButton:
            b = QPushButton(text)
            b.clicked.connect(cb)
            b.setFixedHeight(28)
            return b

        def status(self, msg: str) -> None:
            self.lbl_status.setText(msg)

        def _bump_columns(self, delta: int) -> None:
            self.view.set_column_count(self.view.column_count + delta)

        def _refresh_labels(self) -> None:
            self.lbl_columns.setText(
                f"{self.view.column_count} 列  ·  {self.view.cell_size}px"
            )

        # 外部响应 resize：只做展示 ----
        # 具体的 cell_size 计算在 TileView.resizeEvent → _recalc_grid 内完成，
        # 这里只是把派生值同步到状态栏，展示「窗口宽度 → 格子尺寸」的映射。

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            self._refresh_labels()

        # 信号槽 ----

        def _on_visible_range(self, start: int, end: int, seq: int) -> None:
            self.status(
                f"visible [{start}, {end}]  seq={seq}  "
                f"columns={self.view.column_count}  cell={self.view.cell_size}px"
            )

        def _on_right_click(self, row: int) -> None:
            menu = QMenu(self)
            menu.addAction(f"打开 #{row}")
            menu.addAction(f"收藏 #{row}")
            menu.addSeparator()
            menu.addAction("删除")
            menu.exec(
                self.view.mapToGlobal(self.view.viewport().rect().center())
            )


    def main() -> int:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        w = DemoWindow()
        w.show()
        return app.exec()


    if __name__ == "__main__":
        sys.exit(main())
