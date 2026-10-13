#
""""""

from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, Property
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from .defs import *
from .lru_cache import LRUCache
from .delegate import (
    CellDelegate, CellOption, PixmapCellDelegate, PixmapCacheProtocol
)


class VirtualScrollWidget(QWidget):
    """基于直接绘制的虚拟滚动网格组件。

    信号：
        scroll_changed(int):       滚动偏移量变化，参数为当前 scroll_y。
        unit_clicked(int):         左键单击网格项，参数为全局索引。
        unit_pressed(int):         左键按下网格项。
        unit_right_clicked(int):   右键单击网格项。
        cache_cleared():           LRU 缓存被清空。
        visible_range_changed(int, int, int): 可见范围变化，参数为 (start, end, request_id)。
    """

    scroll_changed = Signal(int)
    unit_clicked = Signal(int)
    unit_pressed = Signal(int)
    unit_right_clicked = Signal(int)
    cache_cleared = Signal()
    visible_range_changed = Signal(int, int, int)

    def __init__(
            self,
            parent: Optional[QWidget] = None,
            *,
            pixmap_cache: Optional[PixmapCacheProtocol] = None,
    ):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # conf
        self.single_row_num = DEFAULT_COLUMN_COUNT
        self.fallback_cell_size = FALLBACK_CELL_SIZE
        self._wheel_pixel_step = WHEEL_PIXEL_STEP
        self.overscroll_top = OVERSCROLL_TOP_MAX
        self._bottom_overscroll = OVERSCROLL_BOTTOM_MAX

        # status
        self._scroll_y = 0.0
        self._max_item_index = MAX_ITEM_INDEX
        self._total_content_height = 0
        self._total_rows = 0
        self._request_counter = 0

        # cache (LRU)
        self._max_cache_size = CACHE_POOL_MAX_ITEM_NUMBER
        self._pixmap_cache: PixmapCacheProtocol = (
            pixmap_cache
            if pixmap_cache is not None
            else LRUCache(self._max_cache_size)
        )

        # 绘制委托（可通过 delegate setter 替换）
        self._delegate: CellDelegate = PixmapCellDelegate(self._pixmap_cache.get)

        # 用于防抖，避免重复发射相同范围
        self._last_emitted_start = -1
        self._last_emitted_end = -1

    # ---- qProperty -------------------------------------------------------

    @Property(int)
    def max_item_index(self) -> int:
        return int(self._max_item_index)

    @max_item_index.setter
    def max_item_index(self, value: int) -> None:
        self.update_max_item_index(value)

    @Property(int)
    def scroll_y(self) -> int:
        return int(self._scroll_y)

    @scroll_y.setter
    def scroll_y(self, value: int) -> None:
        self.set_scroll_y(value)

    @Property(int)
    def column_count(self) -> int:
        return int(self.single_row_num)

    @column_count.setter
    def column_count(self, value: int) -> None:
        self.set_column_count(value)

    # ---- delegate --------------------------------------------------------

    @property
    def delegate(self) -> CellDelegate:
        return self._delegate

    @delegate.setter
    def delegate(self, value: CellDelegate) -> None:
        if value is self._delegate:
            return
        self._delegate = value
        self.update()

    # ---- 过滚 ------------------------------------------------------------

    @property
    def bottom_overscroll(self) -> int:
        return self._bottom_overscroll

    @bottom_overscroll.setter
    def bottom_overscroll(self, value: int) -> None:
        self._bottom_overscroll = max(0, int(value))
        self.set_scroll_y(self._scroll_y)

    # lru cache
    @property
    def pixmap_cache(self) -> PixmapCacheProtocol:
        return self._pixmap_cache

    @pixmap_cache.setter
    def pixmap_cache(self, cache: PixmapCacheProtocol) -> None:
        self._pixmap_cache = cache
        if isinstance(self._delegate, PixmapCellDelegate):
            self._delegate._get_pixmap = cache.get  # 若你给 PixmapCellDelegate 加了 getter setter
        self.update()

    # ---- interface -------------------------------------------------------

    def set_pixmap(self, index: int, pixmap: QPixmap) -> None:
        """设置指定索引处的图像（LRU 缓存自动管理容量）。

        Args:
            index:  网格项全局索引（从 0 开始）。
            pixmap: 要显示的 QPixmap。
        """
        if index < 0:
            return
        self._pixmap_cache[index] = pixmap
        self.update()

    def set_pixmap_batch(self, map_: dict[int, QPixmap]) -> None:
        if any(x < 0 for x in map_):
            return
        try:
            for index in map_:
                self._pixmap_cache[index] = map_[index]
        finally:
            self.update()

    def has_pixmap(self, index: int) -> bool:
        """返回该索引当前是否在 LRU 缓存中（只读探测，不改变访问顺序）。"""
        return index in self._pixmap_cache

    def update_max_item_index(self, index: int, *, force: bool = False) -> None:
        index = max(-1, int(index))

        if not force and index == self._max_item_index:
            return

        self._max_item_index = index
        self._update_total_height()

        if force:
            self._last_emitted_start = -1
            self._last_emitted_end = -1

        self.set_scroll_y(self._scroll_y)
        self.update()
        self._emit_visible_range_if_changed()

    def clear_cache(self) -> None:
        self._pixmap_cache.clear()
        self.update()
        self.cache_cleared.emit()

    def set_scroll_y(self, y: float) -> None:
        max_scroll = self._total_content_height - self.contentsRect().height()
        if max_scroll < 0:
            max_scroll = 0
        # 允许底部过滚：上限 = max_scroll + 底部过滚距离
        clamped = max(-self.overscroll_top,
                      min(y, max_scroll + self._bottom_overscroll))
        clamped = int(clamped)
        if clamped != self._scroll_y:
            self._scroll_y = clamped
            self.scroll_changed.emit(int(self._scroll_y))
            self.update()
            self._emit_visible_range_if_changed()

    def reset_source(self, max_item_index: int) -> None:
        """重置数据源：清空缓存、设置最大索引、滚动回顶部。"""
        self.clear_cache()
        self._max_item_index = max(-1, int(max_item_index))
        self._last_emitted_start = -1
        self._last_emitted_end = -1
        self._update_total_height()
        self.set_scroll_y(0)
        self.update()
        self._emit_visible_range_if_changed()

    def refresh_visible_range(self) -> None:
        """强制重新计算并发射可见范围，忽略防抖。"""
        self._last_emitted_start = -1
        self._last_emitted_end = -1
        self._emit_visible_range_if_changed()

    def set_column_count(self, n: int) -> None:
        n = max(1, min(12, int(n)))
        self._apply_column_count(n)

    def _apply_column_count(self, n: int) -> None:
        n = max(1, int(n))
        if n == self.single_row_num:
            return

        self.single_row_num = n
        self._update_total_height()
        self.set_scroll_y(self._scroll_y)
        self.update()
        self.refresh_visible_range()

    # ---- calc ------------------------------------------------------------

    def _get_cell_size(self) -> int:
        w = self.contentsRect().width()
        if w <= 0:
            return self.fallback_cell_size
        return w // self.single_row_num

    def _index_to_row_col(self, idx: int) -> tuple[int, int]:
        row = idx // self.single_row_num
        col = idx % self.single_row_num
        return row, col

    def _row_col_to_index(self, row: int, col: int) -> int:
        return row * self.single_row_num + col

    def _update_total_height(self) -> None:
        total_items = self._max_item_index + 1 if self._max_item_index is not None else 0
        total_rows = (total_items + self.single_row_num - 1) // self.single_row_num
        cell_sz = self._get_cell_size()
        self._total_content_height = total_rows * cell_sz
        self._total_rows = total_rows

    def _emit_visible_range_if_changed(self) -> None:
        # IMPORTANT: 控件必须在可见且布局完成后才能计算可见范围。
        # 过早调用（如构造阶段）会导致 cell_size 基于默认尺寸计算错误，产生无效的缩略图请求。
        if not self.isVisible():
            return

        if self._total_rows == 0:
            new_start, new_end = 0, 0
        else:
            cell_sz = self._get_cell_size()
            if cell_sz <= 0:
                return
            viewport_h = self.contentsRect().height()
            start_row = max(0, int(self._scroll_y // cell_sz))
            bottom_y = self._scroll_y + viewport_h
            end_row = int((bottom_y + cell_sz - 1) // cell_sz)

            # 如果完全处于过滚区域（无实际内容可见），发射空范围
            if start_row >= self._total_rows or end_row < 0:
                new_start, new_end = 0, 0
            else:
                end_row = min(end_row, self._total_rows - 1)
                new_start = start_row * self.single_row_num
                new_end = min((end_row + 1) * self.single_row_num - 1,
                              self._max_item_index)

        if new_start != self._last_emitted_start or new_end != self._last_emitted_end:
            self._last_emitted_start = new_start
            self._last_emitted_end = new_end
            self._request_counter += 1
            self.visible_range_changed.emit(new_start, new_end,
                                            self._request_counter)

    # ---- draw ------------------------------------------------------------

    def _paint_background(self, painter: QPainter) -> None:
        """在绘制单元格之前调用，子类可覆盖以绘制背景 / 圆角裁剪。

        默认不做任何事。``painter`` 已完成基本 render hint 设置，
        坐标系为 widget 本地坐标系。子类若设置裁剪路径，将作用于后续
        所有单元格绘制。
        """
        pass

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 交给子类绘制背景 / 圆角裁剪（默认空实现）
        self._paint_background(painter)

        cell_sz = self._get_cell_size()
        if cell_sz <= 0:
            return

        rect = self.contentsRect()
        viewport_h = rect.height()

        # 可见行范围（已处理过滚）
        start_row = max(0, int(self._scroll_y // cell_sz))
        bottom_y = self._scroll_y + viewport_h
        end_row = int((bottom_y + cell_sz - 1) // cell_sz)
        if self._total_rows > 0:
            end_row = min(end_row, self._total_rows - 1)
        else:
            end_row = -1

        # 完全处于过滚区域时不绘制
        if start_row > end_row:
            return

        delegate = self._delegate
        for row in range(start_row, end_row + 1):
            for col in range(self.single_row_num):
                idx = self._row_col_to_index(row, col)
                x = col * cell_sz
                y = int(row * cell_sz - self._scroll_y)
                option = CellOption(
                    index=idx,
                    rect=QRect(x, y, cell_sz, cell_sz),
                    cell_size=cell_sz,
                )
                delegate.paint(painter, option)

    # ---- 事件：尺寸 / 显示 ------------------------------------------------

    def resizeEvent(self, event) -> None:
        self._update_total_height()
        super().resizeEvent(event)
        self.update()
        self._emit_visible_range_if_changed()

    def showEvent(self, event):
        super().showEvent(event)
        self._emit_visible_range_if_changed()

    # ---- 事件：输入 ------------------------------------------------------

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # ----- Ctrl + 滚轮：缩放 -----
            delta = event.angleDelta().y()
            if delta == 0:
                event.ignore()
                return

            if delta > 0:
                new_cols = max(1, self.single_row_num - 1)
            else:
                new_cols = min(12, self.single_row_num + 1)

            self.set_column_count(new_cols)
            event.accept()
            return

        # ----- 普通滚轮：垂直滚动 -----
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        step = self._wheel_pixel_step
        new_y = self._scroll_y - delta / 120.0 * step
        self.set_scroll_y(new_y)
        event.accept()

    def mousePressEvent(self, event) -> None:
        """鼠标点击事件，用于识别点击了哪个网格项。"""
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._index_at_pos(event.position().toPoint())
            if idx >= 0:
                self.unit_pressed.emit(idx)
                self.unit_clicked.emit(idx)

        elif event.button() == Qt.MouseButton.RightButton:
            idx = self._index_at_pos(event.position().toPoint())
            if idx >= 0:
                self.unit_right_clicked.emit(idx)
            event.accept()
            return

        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        step = self._wheel_pixel_step
        match event.key():
            case Qt.Key.Key_Down:
                delta_y = step
            case Qt.Key.Key_Up:
                delta_y = -step
            case Qt.Key.Key_PageDown:
                delta_y = self.contentsRect().height()
            case Qt.Key.Key_PageUp:
                delta_y = -self.contentsRect().height()
            case _:
                super().keyPressEvent(event)
                return

        # noinspection PyUnboundLocalVariable
        new_y = self._scroll_y + delta_y
        self.set_scroll_y(new_y)
        event.accept()

    def _index_at_pos(self, pos) -> int:
        cell_sz = self._get_cell_size()
        if cell_sz <= 0:
            return -1

        col = pos.x() // cell_sz
        row = int((pos.y() + self._scroll_y) // cell_sz)

        if col < 0 or col >= self.single_row_num:
            return -1
        if row < 0 or row >= self._total_rows:
            return -1

        idx = self._row_col_to_index(row, col)
        if idx < 0 or idx > self._max_item_index:
            return -1

        return idx
