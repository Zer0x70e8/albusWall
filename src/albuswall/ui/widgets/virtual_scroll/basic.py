#
""""""

from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, Property
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
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
        cursor_changed(int):       键盘光标（当前格）变化，参数为全局索引。
        unit_activated(int):       键盘回车 / 双击激活网格项，参数为全局索引。
        unit_previewed(int):       键盘空格「快速预览」网格项，参数为全局索引。

    键盘操作（纯键盘可用）：
        ←/→/↑/↓      移动光标
        Home/End      行首 / 行尾（Ctrl+Home/End 首项 / 末项）
        PageUp/Down   翻页
        Enter/Return  激活当前项（打开详情等）
        Space         快速预览当前项

    空格与回车在这里**不是**同一个键：回车 = 详情，空格 = 预览。窗口里其它
    控件的「空格 / 回车都算确定」统一由
    :mod:`albuswall.ui.utils.activation_keys` 的应用级过滤器负责，本组件
    不是 ``QAbstractItemView``，因此不受它影响。
    """

    scroll_changed = Signal(int)
    unit_clicked = Signal(int)
    unit_pressed = Signal(int)
    unit_right_clicked = Signal(int)
    cache_cleared = Signal()
    visible_range_changed = Signal(int, int, int)
    cursor_changed = Signal(int)
    unit_activated = Signal(int)
    unit_previewed = Signal(int)

    def __init__(
            self,
            parent: Optional[QWidget] = None,
            *,
            pixmap_cache: Optional[PixmapCacheProtocol] = None,
    ):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 网格自己消费方向键 / Home / End / 回车 / 空格：外部若接入全局
        # 快捷键处理器，看到这个属性就不应再抢走无修饰键。
        self.setProperty("keymap_passthrough", True)

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

        # 键盘光标：当前高亮的网格索引（-1 = 还没有光标 / 无数据）
        self._cursor_index = -1

        # cache (LRU)
        self._max_cache_size = CACHE_POOL_MAX_ITEM_NUMBER
        self._pixmap_cache: PixmapCacheProtocol = (
            pixmap_cache
            if pixmap_cache is not None
            else LRUCache(self._max_cache_size)
        )

        # 绘制委托（可通过 delegate setter 替换）
        self._delegate: CellDelegate = PixmapCellDelegate(self._pixmap_cache.get)

        # 绑定的 Qt 模型（见 bind_model）；None = 纯索引模式
        self._model = None
        self._model_pixmap_role: int = int(Qt.ItemDataRole.DecorationRole)
        self._model_hydrate_visible: bool = True

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
        if self._max_item_index < 0:
            self.set_cursor_index(-1, emit=False)
        elif self._cursor_index < 0:
            # 先有数据后补光标：从第一项开始
            self.set_cursor_index(0, emit=False)
        elif self._cursor_index > self._max_item_index:
            self.set_cursor_index(self._max_item_index, emit=False)
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

    # ---- 模型绑定 --------------------------------------------------------
    #
    # 本控件按「全局索引」工作，不依赖 Qt 的 item view 机制；但外部数据往往
    # 已经是一份 Qt 模型（``AssetGridModel`` 等）。``bind_model`` 就是那座桥：
    # 行数 → ``max_item_index``，``DecorationRole`` → pixmap 缓存，
    # 变更 / 重置 → 自动重新同步，可见范围变化 → 只取看得见的格子。

    def bind_model(
            self,
            model,
            *,
            pixmap_role: int | None = None,
            hydrate_visible: bool = True,
    ) -> "VirtualScrollWidget":
        """直接接收一份 Qt 模型（行数 + 缩略图）并自动保持同步。

        Args:
            model: 任意 ``QAbstractItemModel``；``None`` 表示解绑。
            pixmap_role: 从哪个 role 取缩略图，默认 ``DecorationRole``。
            hydrate_visible: 可见范围变化时是否自动把该范围内的缩略图灌进缓存。

        Returns:
            ``self``，便于链式调用。
        """
        self.unbind_model()
        self._model = model
        self._model_pixmap_role = (
            int(Qt.ItemDataRole.DecorationRole)
            if pixmap_role is None else int(pixmap_role)
        )
        self._model_hydrate_visible = bool(hydrate_visible)
        if model is None:
            return self

        model.rowsInserted.connect(self._on_bound_rows_changed)
        model.rowsRemoved.connect(self._on_bound_rows_changed)
        model.modelReset.connect(self._on_bound_source_changed)
        model.layoutChanged.connect(self._on_bound_source_changed)
        model.dataChanged.connect(self._on_bound_data_changed)
        self.visible_range_changed.connect(self._on_bound_visible_range)
        self._sync_row_count_from_model()
        return self

    def unbind_model(self) -> None:
        """断开与当前模型的全部连接（重复调用安全）。"""
        model = getattr(self, "_model", None)
        if model is None:
            return
        for signal, slot in (
                (model.rowsInserted, self._on_bound_rows_changed),
                (model.rowsRemoved, self._on_bound_rows_changed),
                (model.modelReset, self._on_bound_source_changed),
                (model.layoutChanged, self._on_bound_source_changed),
                (model.dataChanged, self._on_bound_data_changed),
                (self.visible_range_changed, self._on_bound_visible_range),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._model = None

    def model(self):
        """当前绑定的模型（未绑定时为 ``None``）。"""
        return getattr(self, "_model", None)

    def hydrate_model_pixmaps(self, rows=None) -> int:
        """把模型里的缩略图灌进 LRU 缓存，返回本次灌入的数量。

        Args:
            rows: 要处理的行号序列；``None`` 表示当前可见范围。
        """
        model = getattr(self, "_model", None)
        if model is None:
            return 0
        if rows is None:
            rows = self._visible_row_range()

        role = getattr(self, "_model_pixmap_role",
                       int(Qt.ItemDataRole.DecorationRole))
        loaded = 0
        for row in rows:
            if row < 0 or row > self._max_item_index:
                continue
            pixmap = model.index(int(row), 0).data(role)
            if not isinstance(pixmap, QPixmap) or pixmap.isNull():
                continue
            self._pixmap_cache[int(row)] = pixmap
            loaded += 1

        if loaded:
            self.update()
        return loaded

    # ---- 模型绑定的内部实现 ----------------------------------------------

    def _visible_row_range(self):
        start = getattr(self, "_last_emitted_start", -1)
        end = getattr(self, "_last_emitted_end", -1)
        if start < 0 or end < 0:
            return range(0, min(self._max_item_index + 1, 0))
        return range(int(start), int(end) + 1)

    def _sync_row_count_from_model(self) -> None:
        model = getattr(self, "_model", None)
        count = model.rowCount() if model is not None else 0
        self.update_max_item_index(count - 1, force=True)
        self.hydrate_model_pixmaps()

    def _on_bound_rows_changed(self, *_args) -> None:
        self._sync_row_count_from_model()

    def _on_bound_source_changed(self, *_args) -> None:
        self.clear_cache()
        self._sync_row_count_from_model()

    def _on_bound_data_changed(
            self,
            top_left,
            bottom_right,
            roles=None,
    ) -> None:
        role = getattr(self, "_model_pixmap_role",
                       int(Qt.ItemDataRole.DecorationRole))
        if roles and role not in {int(item) for item in roles}:
            decoration = int(Qt.ItemDataRole.DecorationRole)
            if decoration not in {int(item) for item in roles}:
                return
        first = max(0, int(top_left.row()))
        last = min(int(bottom_right.row()), self._max_item_index)
        if last < first:
            return
        self.hydrate_model_pixmaps(range(first, last + 1))

    def _on_bound_visible_range(self, start: int, end: int, _request: int) -> None:
        if getattr(self, "_model_hydrate_visible", False):
            self.hydrate_model_pixmaps(range(int(start), int(end) + 1))

    # ---- 键盘光标 --------------------------------------------------------

    def cursor_index(self) -> int:
        """当前键盘光标所在的全局索引（-1 = 无光标）。"""
        return int(self._cursor_index)

    def set_cursor_index(self, index: int, *, emit: bool = True) -> None:
        """设置键盘光标并滚动到可见处。

        Args:
            index: 目标全局索引；``-1`` 表示清空光标（越界会收敛到合法范围）。
            emit: 是否发射 ``cursor_changed``（数据源重置时不需要）。
        """
        total = self._max_item_index + 1
        if total <= 0:
            new_index = -1
        else:
            new_index = max(0, min(int(index), total - 1))

        if new_index == self._cursor_index:
            if new_index >= 0:
                self._scroll_index_into_view(new_index)
            return

        self._cursor_index = new_index
        if new_index >= 0:
            self._scroll_index_into_view(new_index)
        self.update()
        if emit:
            self.cursor_changed.emit(new_index)

    def move_cursor(self, delta: int) -> None:
        """按项偏移移动光标（越界时停在边界）。"""
        base = self._cursor_index if self._cursor_index >= 0 else 0
        self.set_cursor_index(base + int(delta))

    def activate_cursor(self) -> bool:
        """激活当前光标项（回车语义：打开详情）；没有光标时返回 False。"""
        if 0 <= self._cursor_index <= self._max_item_index:
            self.unit_activated.emit(self._cursor_index)
            return True
        return False

    def preview_cursor(self) -> bool:
        """快速预览当前光标项（空格语义）；没有光标时返回 False。"""
        if 0 <= self._cursor_index <= self._max_item_index:
            self.unit_previewed.emit(self._cursor_index)
            return True
        return False

    def _rows_per_page(self) -> int:
        cell = self._get_cell_size()
        if cell <= 0:
            return 1
        return max(1, self.contentsRect().height() // cell)

    def _scroll_index_into_view(self, index: int) -> None:
        cell = self._get_cell_size()
        if cell <= 0:
            return
        cols = max(1, self.single_row_num)
        top = (index // cols) * cell
        bottom = top + cell
        viewport = self.contentsRect().height()
        y = self._scroll_y
        if top < y:
            y = top
        elif bottom > y + viewport:
            y = bottom - viewport
        self.set_scroll_y(y)

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
        # 数据换了：光标回到第一项（没有数据则清空），避免指向旧索引
        self.set_cursor_index(0 if self._max_item_index >= 0 else -1, emit=False)
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
                    selected=(idx == self._cursor_index),
                    focused=self.hasFocus(),
                )
                delegate.paint(painter, option)

        self._paint_keyboard_focus(painter, start_row, end_row, cell_sz)

    def _paint_keyboard_focus(
            self,
            painter: QPainter,
            start_row: int,
            end_row: int,
            cell_sz: int,
    ) -> None:
        """键盘焦点环：只画当前光标格，且仅在本控件真正持有焦点时。"""
        if not self.hasFocus() or self._cursor_index < 0:
            return
        row, col = divmod(self._cursor_index, max(1, self.single_row_num))
        if row < start_row or row > end_row:
            return
        x = col * cell_sz
        y = int(row * cell_sz - self._scroll_y)
        rect = QRect(x + 2, y + 2, cell_sz - 4, cell_sz - 4)
        pen = QPen(QColor(0, 122, 255, 235))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 6, 6)

    # ---- 事件：焦点 / 尺寸 / 显示 -----------------------------------------

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

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
                self.set_cursor_index(idx)
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
        """纯键盘操作：移动光标 / 翻页 / 激活当前项。"""
        total = self._max_item_index + 1
        if total <= 0:
            super().keyPressEvent(event)
            return

        cols = max(1, self.single_row_num)
        current = self._cursor_index if self._cursor_index >= 0 else 0
        key = event.key()
        target: Optional[int] = None

        if key == Qt.Key.Key_Left:
            target = current - 1
        elif key == Qt.Key.Key_Right:
            target = current + 1
        elif key == Qt.Key.Key_Up:
            target = current - cols
        elif key == Qt.Key.Key_Down:
            target = current + cols
        elif key == Qt.Key.Key_Home:
            target = 0
        elif key == Qt.Key.Key_End:
            target = total - 1
        elif key == Qt.Key.Key_PageUp:
            target = current - cols * self._rows_per_page()
        elif key == Qt.Key.Key_PageDown:
            target = current + cols * self._rows_per_page()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.activate_cursor():
                event.accept()
                return
            super().keyPressEvent(event)
            return
        elif key == Qt.Key.Key_Space:
            # 内容视图里空格是「预览」、回车是「详情」：两个键分开，不合并。
            if self.preview_cursor():
                event.accept()
                return
            super().keyPressEvent(event)
            return
        else:
            super().keyPressEvent(event)
            return

        if target is not None:
            self.set_cursor_index(target)
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
