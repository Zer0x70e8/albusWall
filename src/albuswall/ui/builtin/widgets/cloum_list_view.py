#
""""""

from PySide6.QtGui import QPainter, QRegion, QWheelEvent
from PySide6.QtWidgets import QAbstractItemView, QStyleOptionViewItem, QStyle
from PySide6.QtCore import (
    Qt, QRect, QPoint, QModelIndex,
    QItemSelectionModel, QItemSelection, QPersistentModelIndex
)

WHEEL_INVERTED: bool = False


class ColumnLayoutCalculator:
    """多列网格布局计算器。

    职责：
      - 均匀列宽分配
      - 索引 -> 视口矩形
      - 可视行范围（视口裁剪）
      - 内容总高度

    所有「滚动」输入都是垂直滚动条的 value（= 内容已向上滚过的像素数）。
    """

    def __init__(self, column_count=1, item_height=160, spacing=5, margin=10):
        self.column_count = max(1, int(column_count))
        self.item_height = int(item_height)
        self.spacing = int(spacing)
        self.margin = int(margin)

    # ---------- 基础度量 ----------
    def row_stride(self) -> int:
        return self.item_height + self.spacing

    def grid_row_count(self, model) -> int:
        if not model:
            return 0
        total = model.rowCount()
        if total <= 0:
            return 0
        return (total + self.column_count - 1) // self.column_count

    def column_metrics(self, viewport_width: int):
        """返回 (col_xs, col_ws) 或 None（宽度不足以放下 1 列）。"""
        available = (viewport_width
                     - 2 * self.margin
                     - (self.column_count - 1) * self.spacing)
        if available <= 0:
            return None
        base_w = available // self.column_count
        remainder = available % self.column_count
        xs, ws = [], []
        x = self.margin
        for c in range(self.column_count):
            w = base_w + (1 if c < remainder else 0)
            xs.append(x)
            ws.append(w)
            x += w + self.spacing
        return xs, ws

    # ---------- 可见范围（视口裁剪核心） ----------
    def visible_grid_rows(self, viewport_height: int,
                          scroll_offset: int, grid_rows: int):
        """返回 [first, last) 半开区间的可见 grid row，带 1 行上下缓冲。"""
        if grid_rows <= 0:
            return 0, 0
        stride = self.row_stride()
        # 内容 y 坐标：grid_row 的顶部 = margin + grid_row * stride
        top_content = scroll_offset - self.margin
        bottom_content = scroll_offset + viewport_height - self.margin
        first = 0 if top_content < 0 else (top_content // stride) - 1
        first = max(0, first)
        last = min(grid_rows, bottom_content // stride + 2)
        return first, last

    # ---------- 矩形 ----------
    def rect_for_index(self, model, index: QModelIndex,
                       viewport_width: int, scroll_offset: int) -> QRect:
        """只计算单个索引的矩形（O(1)，不做全量遍历）。"""
        if not model or not index.isValid():
            return QRect()
        metrics = self.column_metrics(viewport_width)
        if metrics is None:
            return QRect()
        xs, ws = metrics
        row = index.row()
        grid_row = row // self.column_count
        grid_col = row % self.column_count
        y = self.margin + grid_row * self.row_stride() - scroll_offset
        return QRect(xs[grid_col], y, ws[grid_col], self.item_height)

    def compute_rects(self, model, viewport_rect: QRect,
                      scroll_offset: int, visible_only: bool = True):
        """返回 {QModelIndex: QRect}。

        visible_only=True 时只遍历视口内的行（含 1 行缓冲），
        避免对大模型做 O(N) 全量遍历。
        """
        rects = {}
        if not model or model.rowCount() == 0:
            return rects
        metrics = self.column_metrics(viewport_rect.width())
        if metrics is None:
            return rects
        xs, ws = metrics
        total = model.rowCount()
        grid_rows = (total + self.column_count - 1) // self.column_count

        if visible_only:
            first, last = self.visible_grid_rows(
                viewport_rect.height(), scroll_offset, grid_rows)
        else:
            first, last = 0, grid_rows

        stride = self.row_stride()
        for grid_row in range(first, last):
            y = self.margin + grid_row * stride - scroll_offset
            base = grid_row * self.column_count
            for col in range(self.column_count):
                row = base + col
                if row >= total:
                    break
                rects[model.index(row, 0)] = QRect(
                    xs[col], y, ws[col], self.item_height)
        return rects

    def total_height(self, model) -> int:
        if not model or model.rowCount() == 0:
            return 0
        grid_rows = (model.rowCount() + self.column_count - 1) // self.column_count
        return (self.margin * 2
                + grid_rows * self.item_height
                + (grid_rows - 1) * self.spacing)


class ColumnListView(QAbstractItemView):
    """多列卡片视图。

    特性：
      - 均匀列宽、自动布局
      - 滚轮方向可反转
      - 精确命中检测（indexAt）
      - 支持 Ctrl / Shift 多选（由 selection_mode 决定最终行为）
      - 大模型下仅绘制视口内的项，滚动流畅
      - 悬停高亮只重绘发生变化的项

    用法::

        view = ColumnListView(item_height=160, spacing=5, margin=10)
        view.setModel(model)
        view.setItemDelegate(MyCardDelegate())
        view.set_column_count(3)
    """

    def __init__(
            self,
            parent=None,
            item_height: int = 160,
            spacing: int = 5,
            margin: int = 10,
            selection_mode: QAbstractItemView.SelectionMode
            = QAbstractItemView.SelectionMode.SingleSelection,
    ):
        super().__init__(parent)
        self._column_count = 1
        self._wheel_inverted = WHEEL_INVERTED
        self._hovered_index = QModelIndex()
        self._layout_calc = ColumnLayoutCalculator(
            self._column_count,
            item_height=item_height,
            spacing=spacing,
            margin=margin,
        )

        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSelectionMode(selection_mode)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName(type(self).__name__)

    # ---------- 只读属性 ----------
    @property
    def item_height(self) -> int:
        return self._layout_calc.item_height

    @property
    def spacing(self) -> int:
        return self._layout_calc.spacing

    @property
    def margin(self) -> int:
        return self._layout_calc.margin

    # ---------- 滚动偏移 ----------
    def _scroll_offset(self) -> int:
        """内容已向上滚过的像素数，与命中测试/绘制保持一致。"""
        return self.verticalScrollBar().value()

    # ---------- 滚轮 ----------
    def wheelEvent(self, event: QWheelEvent):
        if not self.model():
            return
        delta = event.angleDelta().y()
        if self._wheel_inverted:
            delta = -delta
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() - delta)
        event.accept()

    def set_wheel_inverted(self, inverted: bool):
        self._wheel_inverted = bool(inverted)

    def is_wheel_inverted(self) -> bool:
        return self._wheel_inverted

    # ---------- 列数 ----------
    def column_count(self) -> int:
        return self._column_count

    def set_column_count(self, count: int):
        count = max(1, int(count))
        if count == self._column_count:
            return
        self._column_count = count
        self._layout_calc.column_count = count
        self.updateGeometries()
        self.viewport().update()

    # ---------- 基类虚函数 ----------
    def horizontalOffset(self) -> int:
        return 0

    def verticalOffset(self) -> int:
        return self._scroll_offset()

    def isIndexHidden(self, index: QModelIndex | QPersistentModelIndex) -> bool:
        ...

    def visualRect(self, index: QModelIndex | QPersistentModelIndex) -> QRect:
        ...

    def sizeHintForRow(self, row: int) -> int:
        return self._layout_calc.item_height

    def sizeHintForColumn(self, column: int) -> int:
        # 使用自定义列宽，不向基类提供列宽提示
        return -1

    # ========== 悬停状态管理（最小重绘） ==========
    def _set_hovered_index(self, index: QModelIndex):
        if self._hovered_index == index:
            return
        old = self._hovered_index
        self._hovered_index = index
        if old.isValid():
            r = self.visualRect(old)
            if not r.isEmpty():
                self.viewport().update(r)
        if index.isValid():
            r = self.visualRect(index)
            if not r.isEmpty():
                self.viewport().update(r)

    def leaveEvent(self, event):
        self._set_hovered_index(QModelIndex())
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._set_hovered_index(self.indexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    # ========== 键盘光标移动 ==========
    def moveCursor(self, cursor_action, modifiers):
        if not self.model() or self.model().rowCount() == 0:
            return QModelIndex()

        current = self.currentIndex()
        if not current.isValid():
            return self.model().index(0, 0)

        total = self.model().rowCount()
        row = current.row()
        cols = self._column_count

        # 兼容 int / KeyboardModifier 两种输入，避开类型告警
        mods_value = getattr(modifiers, "value", modifiers)
        ctrl_value = Qt.KeyboardModifier.ControlModifier.value
        ctrl = bool(mods_value & ctrl_value)

        if cursor_action == QAbstractItemView.CursorAction.MoveUp:
            new_row = row - cols
            if new_row < 0:
                return QModelIndex()
        elif cursor_action == QAbstractItemView.CursorAction.MoveDown:
            new_row = row + cols
            if new_row >= total:
                return QModelIndex()
        elif cursor_action == QAbstractItemView.CursorAction.MoveLeft:
            new_row = row - 1
            if new_row < 0:
                return QModelIndex()
        elif cursor_action == QAbstractItemView.CursorAction.MoveRight:
            new_row = row + 1
            if new_row >= total:
                return QModelIndex()
        elif cursor_action == QAbstractItemView.CursorAction.MoveHome:
            if ctrl:
                new_row = 0
            else:
                visual_row = row // cols
                new_row = visual_row * cols
        elif cursor_action == QAbstractItemView.CursorAction.MoveEnd:
            if ctrl:
                new_row = total - 1
            else:
                visual_row = row // cols
                new_row = min(visual_row * cols + cols - 1, total - 1)
        else:
            return QModelIndex()

        return self.model().index(new_row, 0)

    # ========== 命中测试 ==========
    def indexAt(self, point: QPoint) -> QModelIndex:
        if not self.model() or self.model().rowCount() == 0:
            return QModelIndex()

        calc = self._layout_calc
        if point.x() < calc.margin:
            return QModelIndex()

        metrics = calc.column_metrics(self.viewport().width())
        if metrics is None:
            return QModelIndex()
        xs, ws = metrics

        col = -1
        for c, w in enumerate(ws):
            if xs[c] <= point.x() < xs[c] + w:
                col = c
                break
        if col == -1:
            return QModelIndex()

        content_y = point.y() + self._scroll_offset() - calc.margin
        if content_y < 0:
            return QModelIndex()

        stride = calc.row_stride()
        grid_row = content_y // stride
        if content_y - grid_row * stride >= calc.item_height:
            return QModelIndex()

        row = grid_row * self._column_count + col
        if row >= self.model().rowCount():
            return QModelIndex()
        return self.model().index(row, 0)

    # ========== 鼠标选择 ==========
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.model():
            super().mousePressEvent(event)
            return

        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position().toPoint()
        index = self.indexAt(point)

        # 用 .value 拿 int，避免 int(KeyboardModifier) 触发类型告警
        mods_value = event.modifiers().value
        ctrl_value = Qt.KeyboardModifier.ControlModifier.value
        shift_value = Qt.KeyboardModifier.ShiftModifier.value

        has_ctrl = bool(mods_value & ctrl_value)
        has_shift = bool(mods_value & shift_value)

        sel_model = self.selectionModel()

        if not index.isValid():
            if not (has_ctrl or has_shift):
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
            self.viewport().update()
            event.accept()
            return

        sf = QItemSelectionModel.SelectionFlag

        if has_ctrl:
            sel_model.select(index, sf.Toggle)
            self.setCurrentIndex(index)
        elif has_shift and self.currentIndex().isValid():
            current = self.currentIndex()
            step = 1 if index.row() >= current.row() else -1
            selection = QItemSelection()
            for r in range(current.row(), index.row() + step, step):
                idx = self.model().index(r, 0)
                selection.select(idx, idx)
            sel_model.select(selection, sf.ClearAndSelect)
        else:
            sel_model.select(index, sf.ClearAndSelect)
            self.setCurrentIndex(index)

        self.viewport().update()
        event.accept()

    # ========== 滚动到指定索引 ==========
    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        if not index.isValid() or not self.model():
            return
        rect = self.visualRect(index)
        if rect.isNull():
            return

        viewport_h = self.viewport().height()
        scroll_offset = self._scroll_offset()
        target = scroll_offset

        if rect.top() < 0:
            target += rect.top()
        elif rect.bottom() > viewport_h:
            target += rect.bottom() - viewport_h

        target = max(0, target)
        if target != scroll_offset:
            self.verticalScrollBar().setValue(target)

    # ========== 框选 ==========
    def setSelection(self, rect: QRect,
                     command: QItemSelectionModel.SelectionFlag):
        if not self.model() or not self.selectionModel():
            return

        # 只需要与视口相交的项（rect 本身是视口坐标系）
        rects = self._layout_calc.compute_rects(
            self.model(),
            self.viewport().rect(),
            self._scroll_offset(),
            visible_only=True,
        )
        selected = [idx for idx, r in rects.items() if r.intersects(rect)]
        if not selected:
            return

        selection = QItemSelection()
        for idx in selected:
            selection.select(idx, idx)
        self.selectionModel().select(selection, command)

    def visualRegionForSelection(self, selection) -> QRegion:
        region = QRegion()
        for index in selection.indexes():
            rect = self.visualRect(index)
            if not rect.isEmpty():
                region += QRegion(rect)
        return region

    # ========== 滚动条变化 ==========
    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.viewport().update()

    # ========== 更新滚动条范围 ==========
    def updateGeometries(self):
        super().updateGeometries()
        sb = self.verticalScrollBar()
        if not self.model():
            sb.setRange(0, 0)
            return

        total_height = self._layout_calc.total_height(self.model())
        viewport_h = self.viewport().height()
        max_offset = max(0, total_height - viewport_h)

        sb.setRange(0, max_offset)
        sb.setPageStep(viewport_h)
        sb.setSingleStep(20)
        self.viewport().update()

    # ========== 绘制（只画视口内的项） ==========
    def paintEvent(self, event):
        if not self.model():
            return

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rects = self._layout_calc.compute_rects(
            self.model(),
            self.viewport().rect(),
            self._scroll_offset(),
            visible_only=True,
        )

        delegate = self.itemDelegate()
        sel_model = self.selectionModel()
        current = self.currentIndex()
        hovered = self._hovered_index

        for index, rect in rects.items():
            option = QStyleOptionViewItem()
            option.rect = rect
            option.widget = self
            option.state = QStyle.StateFlag.State_Enabled

            if sel_model and sel_model.isSelected(index):
                option.state |= QStyle.StateFlag.State_Selected
            if current == index:
                option.state |= QStyle.StateFlag.State_HasFocus
            if hovered == index:
                option.state |= QStyle.StateFlag.State_MouseOver

            delegate.paint(painter, option, index)

        painter.end()

    # ========== 模型变化处理（显式信号连接） ==========
    def setModel(self, model):
        old = self.model()
        if old is not None:
            for sig, slot in (
                    (old.dataChanged, self._on_data_changed),
                    (old.rowsInserted, self._on_rows_changed),
                    (old.rowsRemoved, self._on_rows_changed),
                    (old.modelReset, self._on_model_reset),
                    (old.layoutChanged, self._on_model_reset),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        super().setModel(model)

        if model is not None:
            model.dataChanged.connect(self._on_data_changed)
            model.rowsInserted.connect(self._on_rows_changed)
            model.rowsRemoved.connect(self._on_rows_changed)
            model.modelReset.connect(self._on_model_reset)
            model.layoutChanged.connect(self._on_model_reset)

        self._hovered_index = QModelIndex()
        self.verticalScrollBar().setValue(0)
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())
        self.updateGeometries()
        self.viewport().update()

    def _on_data_changed(self, *_):
        self.updateGeometries()
        self.viewport().update()

    def _on_rows_changed(self, *_):
        self.updateGeometries()
        self.viewport().update()

    def _on_model_reset(self, *_):
        self._hovered_index = QModelIndex()
        self.verticalScrollBar().setValue(0)
        self.updateGeometries()
        self.viewport().update()

    # ========== 尺寸变化 ==========
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updateGeometries()
