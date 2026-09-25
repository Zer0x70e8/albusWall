#
""""""

from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, Property
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

try:
    from .defs import *
    from .lru_cache import LRUCache
except ImportError:
    from defs import *
    from lru_cache import LRUCache


class VirtualScrollWidget(QWidget):
    """基于直接绘制的虚拟滚动网格组件。

    信号：
        scroll_changed(int): 当滚动偏移量发生变化时发射，参数为当前的 scroll_y。
        unit_clicked(int):   当用户点击某个网格项时发射，参数为该项的全局索引。
    """
    scroll_changed = Signal(int)
    unit_clicked = Signal(int)
    cache_cleared = Signal()
    visible_range_changed = Signal(int, int, int)  # start, end, request_id

    def __init__(self, parent: Optional[QWidget] = None):
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
        self._pixmap_cache: LRUCache[int, QPixmap] = LRUCache(self._max_cache_size)
        # 用于防抖，避免重复发射相同范围
        self._last_emitted_start = -1
        self._last_emitted_end = -1

        # #
        # self._update_total_height()

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
        self._apply_column_count(value)

    # interface
    def set_pixmap(self, index: int, pixmap: QPixmap) -> None:
        """设置指定索引处的图像（LRU 缓存自动管理容量）。

        Args:
            index:  网格项全局索引（从 0 开始）。
            pixmap: 要显示的 QPixmap。
        """
        if index < 0:
            return

        # LRU 缓存自动处理容量淘汰，不需要手动检查
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
        """返回该索引当前是否在 LRU 缓存中（只读探测，不改变访问顺序）。

        注意：LRU 会静默淘汰，任何外部维护的"已加载"集合都会失真，
        必须以本方法为准。
        """
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

        # 总高度变化后可能需要对 scroll_y 重新钳位
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

    #
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

    def _apply_column_count(self, n: int) -> None:
        n = max(1, int(n))
        if n == self.single_row_num:
            return

        self.single_row_num = n
        self._update_total_height()
        self.set_scroll_y(self._scroll_y)
        self.update()
        self.refresh_visible_range()

    # calc
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

    # draw
    def on_no_img_draw(self, painter):
        pass

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cell_sz = self._get_cell_size()
        if cell_sz <= 0:
            return

        rect = self.contentsRect()
        viewport_h = rect.height()

        # 可见行范围（已处理过滚）
        start_row = max(0, int(self._scroll_y // cell_sz))
        bottom_y = self._scroll_y + viewport_h
        end_row = int((bottom_y + cell_sz - 1) // cell_sz)
        end_row = min(end_row, self._total_rows - 1)

        # 完全处于过滚区域时不绘制
        if start_row > end_row:
            return

        for row in range(start_row, end_row + 1):
            for col in range(self.single_row_num):
                idx = self._row_col_to_index(row, col)
                if idx in self._pixmap_cache:
                    pixmap = self._pixmap_cache[idx]
                    x = col * cell_sz
                    y = int(row * cell_sz - self._scroll_y)
                    target_rect = QRect(x, y, cell_sz, cell_sz)
                    painter.drawPixmap(target_rect, pixmap)
                else:
                    self.on_no_img_draw(painter)

    # event
    def resizeEvent(self, event) -> None:
        self._update_total_height()
        super().resizeEvent(event)
        self.update()
        self._emit_visible_range_if_changed()

    def showEvent(self, event):
        super().showEvent(event)
        self._emit_visible_range_if_changed()
