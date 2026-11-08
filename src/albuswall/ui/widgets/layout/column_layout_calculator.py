#
"""多列网格布局计算器（ColumnLayoutCalculator）。

只做纯几何计算（列宽、可见行范围、item 矩形），不碰视图状态，
方便被 :class:`~albuswall.ui.widgets.view.column_list_view.ColumnListView`
以及任何需要同款网格度量的地方复用。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect


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
