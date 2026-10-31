#
""""""

from PySide6.QtGui import (
    QPainter, QRegion, QWheelEvent, QFontMetrics,
    QPalette,  # noqa: F401  (仅用于类型提示/可读性)
)
from PySide6.QtWidgets import QAbstractItemView, QStyleOptionViewItem, QStyle
from PySide6.QtCore import (
    Qt, QRect, QPoint, QModelIndex,
    QItemSelectionModel, QItemSelection, QPersistentModelIndex, Property
)

WHEEL_INVERTED: bool = False

CA = QAbstractItemView.CursorAction
SF = QItemSelectionModel.SelectionFlag
StateFlag = QStyle.StateFlag
S_Enabled = StateFlag.State_Enabled
S_Selected = StateFlag.State_Selected
S_Focus = StateFlag.State_HasFocus
S_Mouse = StateFlag.State_MouseOver


def _card_prop(attr: str, default: int) -> Property:
    """生成 int 型 Q_PROPERTY，QSS 里用 qproperty-<名字> 设置。"""
    storage = f"_card_{attr}"

    def _get(self) -> int:
        return getattr(self, storage, default)

    def _set(self, value) -> None:
        value = int(value)
        if getattr(self, storage, default) == value:
            return
        setattr(self, storage, value)
        vp = self.viewport()
        if vp is not None:
            vp.update()

    return Property(int, _get, _set)


class ColumnLayoutCalculator:
    """多列网格布局计算器（与之前一致，做了少量缓存与边界清理）。"""

    __slots__ = ("column_count", "item_height", "spacing", "margin")

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
        available = (viewport_width
                     - 2 * self.margin
                     - (self.column_count - 1) * self.spacing)
        if available <= 0:
            return None
        base_w, remainder = divmod(available, self.column_count)
        xs, ws = [], []
        x = self.margin
        for c in range(self.column_count):
            w = base_w + (1 if c < remainder else 0)
            xs.append(x)
            ws.append(w)
            x += w + self.spacing
        return xs, ws

    # ---------- 可见范围 ----------
    def visible_grid_rows(self, viewport_height: int,
                          scroll_offset: int, grid_rows: int):
        if grid_rows <= 0:
            return 0, 0
        stride = self.row_stride()
        top_content = scroll_offset - self.margin
        bottom_content = scroll_offset + viewport_height - self.margin
        first = 0 if top_content < 0 else max(0, top_content // stride - 1)
        last = min(grid_rows, bottom_content // stride + 2)
        return first, last

    # ---------- 矩形 ----------
    def rect_for_index(self, model, index: QModelIndex,
                       viewport_width: int, scroll_offset: int) -> QRect:
        if not model or not index.isValid():
            return QRect()
        metrics = self.column_metrics(viewport_width)
        if metrics is None:
            return QRect()
        xs, ws = metrics
        row = index.row()
        grid_row, grid_col = divmod(row, self.column_count)
        y = self.margin + grid_row * self.row_stride() - scroll_offset
        return QRect(xs[grid_col], y, ws[grid_col], self.item_height)

    def compute_rects(self, model, viewport_rect: QRect,
                      scroll_offset: int, visible_only: bool = True):
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
        cols = self.column_count
        for grid_row in range(first, last):
            y = self.margin + grid_row * stride - scroll_offset
            base = grid_row * cols
            # 只取本行的有效列数，避免最后一行越界判断
            ncols = min(cols, total - base)
            for col in range(ncols):
                rects[model.index(base + col, 0)] = QRect(
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
    """多列卡片视图（原文档字符串保留）。"""

    # ============ QSS 可调参数 ============
    cardRadius = _card_prop("radius", 10)
    cardPadding = _card_prop("padding", 12)
    cardGap = _card_prop("gap", 6)
    chipHeight = _card_prop("chipHeight", 22)
    chipPadX = _card_prop("chipPadX", 9)
    chipGap = _card_prop("chipGap", 6)
    maxDescLines = _card_prop("maxDescLines", 3)
    cardMinWidth = _card_prop("cardMinWidth", 220)
    cardMinHeight = _card_prop("cardMinHeight", 176)

    borderAlpha = _card_prop("borderAlpha", 12)
    hoverAlpha = _card_prop("hoverAlpha", 4)
    hoverBorderAlpha = _card_prop("hoverBorderAlpha", 30)
    selectAlpha = _card_prop("selectAlpha", 10)
    descAlpha = _card_prop("descAlpha", 78)
    pathAlpha = _card_prop("pathAlpha", 50)
    chipBgAlpha = _card_prop("chipBgAlpha", 16)

    CARD_DEFAULTS = {
        "cardRadius": 10, "cardPadding": 12, "cardGap": 6,
        "chipHeight": 22, "chipPadX": 9, "chipGap": 6,
        "maxDescLines": 3, "cardMinWidth": 220, "cardMinHeight": 176,
        "borderAlpha": 12, "hoverAlpha": 4, "hoverBorderAlpha": 30,
        "selectAlpha": 10, "descAlpha": 78, "pathAlpha": 50, "chipBgAlpha": 16,
    }

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
        # 缓存：视图字体变化时在 changeEvent 里刷新
        self._cached_fm: QFontMetrics | None = None
        self._cached_font_key = None

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
        return self.verticalScrollBar().value()

    # ---------- 字体度量缓存 ----------
    def _font_metrics(self) -> QFontMetrics:
        f = self.font()
        key = (f.family(), f.pointSizeF(), f.weight(), f.italic())
        if key != self._cached_font_key or self._cached_fm is None:
            self._cached_fm = QFontMetrics(f)
            self._cached_font_key = key
        # noinspection bad-return
        return self._cached_fm

    def changeEvent(self, event):
        super().changeEvent(event)
        # 字体变化时使缓存失效
        if event.type() in (
                event.Type.FontChange, event.Type.StyleChange,
                event.Type.PaletteChange,
        ):
            self._cached_fm = None
            self._cached_font_key = None

    # ---------- 滚轮（支持触控板 pixelDelta） ----------
    def wheelEvent(self, event: QWheelEvent):
        if not self.model():
            return
        sb = self.verticalScrollBar()
        angle = event.angleDelta().y()
        pixel = event.pixelDelta().y()
        if self._wheel_inverted:
            angle = -angle
            pixel = -pixel
        if angle:
            sb.setValue(sb.value() - angle)
        elif pixel:
            sb.setValue(sb.value() - pixel)
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

    def visualRect(self, index) -> QRect:
        model = self.model()
        if not model or not index.isValid():
            return QRect()
        # QPersistentModelIndex 需要转 QModelIndex
        if isinstance(index, QPersistentModelIndex):
            index = model.index(index.row(), index.column())
        return self._layout_calc.rect_for_index(
            model, index, self.viewport().width(), self._scroll_offset())

    def isIndexHidden(self, index) -> bool:
        return False

    def sizeHintForRow(self, row: int) -> int:
        return self._layout_calc.item_height

    def sizeHintForColumn(self, column: int) -> int:
        return -1

    # ========== 悬停 ==========
    def _set_hovered_index(self, index: QModelIndex):
        if self._hovered_index == index:
            return
        old = self._hovered_index
        self._hovered_index = index
        vp = self.viewport()
        if old.isValid():
            r = self.visualRect(old)
            if not r.isEmpty():
                vp.update(r)
        if index.isValid():
            r = self.visualRect(index)
            if not r.isEmpty():
                vp.update(r)

    def leaveEvent(self, event):
        self._set_hovered_index(QModelIndex())
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._set_hovered_index(self.indexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    # ========== 键盘移动 ==========
    def moveCursor(self, cursor_action, modifiers):
        model = self.model()
        if not model or model.rowCount() == 0:
            return QModelIndex()

        current = self.currentIndex()
        if not current.isValid():
            return model.index(0, 0)

        total = model.rowCount()
        row = current.row()
        cols = self._column_count

        mods_value = getattr(modifiers, "value", modifiers)
        ctrl = bool(mods_value & Qt.KeyboardModifier.ControlModifier.value)

        if cursor_action == CA.MoveUp:
            new_row = row - cols
            if new_row < 0:
                return QModelIndex()
        elif cursor_action == CA.MoveDown:
            new_row = row + cols
            if new_row >= total:
                return QModelIndex()
        elif cursor_action == CA.MoveLeft:
            new_row = row - 1
            if new_row < 0:
                return QModelIndex()
        elif cursor_action == CA.MoveRight:
            new_row = row + 1
            if new_row >= total:
                return QModelIndex()
        elif cursor_action == CA.MoveHome:
            new_row = 0 if ctrl else (row // cols) * cols
        elif cursor_action == CA.MoveEnd:
            if ctrl:
                new_row = total - 1
            else:
                new_row = min((row // cols) * cols + cols - 1, total - 1)
        else:
            return QModelIndex()

        return model.index(new_row, 0)

    # ========== 命中测试 ==========
    def indexAt(self, point: QPoint) -> QModelIndex:
        model = self.model()
        if not model or model.rowCount() == 0:
            return QModelIndex()

        calc = self._layout_calc
        if point.x() < calc.margin:
            return QModelIndex()

        metrics = calc.column_metrics(self.viewport().width())
        if metrics is None:
            return QModelIndex()
        xs, ws = metrics

        col = -1
        px = point.x()
        for c in range(len(ws)):
            if xs[c] <= px < xs[c] + ws[c]:
                col = c
                break
        if col == -1:
            return QModelIndex()

        content_y = point.y() + self._scroll_offset() - calc.margin
        if content_y < 0:
            return QModelIndex()

        stride = calc.row_stride()
        grid_row, rem = divmod(content_y, stride)
        if rem >= calc.item_height:
            return QModelIndex()

        row = grid_row * self._column_count + col
        if row >= model.rowCount():
            return QModelIndex()
        return model.index(row, 0)

    # ========== 鼠标选择 ==========
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.model():
            super().mousePressEvent(event)
            return

        self.setFocus(Qt.FocusReason.MouseFocusReason)
        index = self.indexAt(event.position().toPoint())

        mods_value = event.modifiers().value
        has_ctrl = bool(mods_value & Qt.KeyboardModifier.ControlModifier.value)
        has_shift = bool(mods_value & Qt.KeyboardModifier.ShiftModifier.value)

        sel_model = self.selectionModel()

        if not index.isValid():
            if not (has_ctrl or has_shift):
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
            self.viewport().update()
            event.accept()
            return

        if has_ctrl:
            sel_model.select(index, SF.Toggle)
            self.setCurrentIndex(index)
        elif has_shift and self.currentIndex().isValid():
            # ★ 用范围选择替代逐行循环，O(1) 构造选择对象
            current = self.currentIndex()
            lo, hi = sorted((current.row(), index.row()))
            model = self.model()
            selection = QItemSelection()
            selection.select(model.index(lo, 0), model.index(hi, 0))
            sel_model.select(selection, SF.ClearAndSelect)
        else:
            sel_model.select(index, SF.ClearAndSelect)
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

        rects = self._layout_calc.compute_rects(
            self.model(),
            self.viewport().rect(),
            self._scroll_offset(),
            visible_only=True,
        )
        selection = QItemSelection()
        for idx, r in rects.items():
            if r.intersects(rect):
                selection.select(idx, idx)
        if selection.isEmpty():
            return
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

    # ========== 更新滚动范围 ==========
    def updateGeometries(self):
        super().updateGeometries()
        sb = self.verticalScrollBar()
        model = self.model()
        if not model:
            sb.setRange(0, 0)
            return

        total_height = self._layout_calc.total_height(model)
        viewport_h = self.viewport().height()
        max_offset = max(0, total_height - viewport_h)

        sb.setRange(0, max_offset)
        sb.setPageStep(viewport_h)
        sb.setSingleStep(20)
        self.viewport().update()

    # ========== 绘制（只画视口内的项） ==========
    def paintEvent(self, event):
        model = self.model()
        if not model:
            return

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rects = self._layout_calc.compute_rects(
            model,
            self.viewport().rect(),
            self._scroll_offset(),
            visible_only=True,
        )
        if not rects:
            painter.end()
            return

        delegate = self.itemDelegate()
        sel_model = self.selectionModel()
        current = self.currentIndex()
        hovered = self._hovered_index

        # ★ 缓存 palette / font / fontMetrics / direction —— 避免每个 item 重复构造
        base_palette = self.palette()
        base_font = self.font()
        base_fm = self._font_metrics()
        base_dir = self.layoutDirection()

        for index, rect in rects.items():
            option = QStyleOptionViewItem()
            option.palette = base_palette
            option.font = base_font
            option.fontMetrics = base_fm
            option.direction = base_dir
            option.rect = rect
            option.widget = self

            state = S_Enabled
            if sel_model and sel_model.isSelected(index):
                # noinspection unsupported-operator
                state |= S_Selected
            if current == index:
                # noinspection unsupported-operator
                state |= S_Focus
            if hovered == index:
                # noinspection unsupported-operator
                state |= S_Mouse
            option.state = state

            delegate.paint(painter, option, index)

        painter.end()

    # ========== 模型变化处理 ==========
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

    def _on_data_changed(self, top_left, bottom_right):
        # ★ 行高固定，无需 updateGeometries；只重绘受影响的矩形
        model = self.model()
        if not model:
            return
        last = min(bottom_right.row(), model.rowCount() - 1)
        vp = self.viewport()
        for r in range(top_left.row(), last + 1):
            rect = self.visualRect(model.index(r, 0))
            if not rect.isEmpty():
                vp.update(rect)

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
