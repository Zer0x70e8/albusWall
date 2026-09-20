#
""""""

from typing import Optional

from PySide6.QtCore import QRectF, Property
from PySide6.QtGui import QPainter, QPainterPath, QPalette, QColor
from PySide6.QtWidgets import QWidget

try:
    from .basic import VirtualScrollWidget
except ImportError:
    from basic import VirtualScrollWidget


class StyledVirtualScrollWidget(VirtualScrollWidget):
    """支持圆角、调色板着色与过滚背景的虚拟滚动组件。

    额外特性：
    - border_radius: 控件圆角半径（像素）
    - 使用 QPalette 定义颜色：
        - Window 角色：过滚区域及整体背景色
        - Base 角色：空白单元格的填充色
    - 支持底部过滚（与顶部对称），以便在过滚时显示背景色
    """

    def __init__( self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._border_radius = 0
        # self._placeholder_color = QColor(200, 200, 200)
        self._placeholder_color = QColor(0,0,0,0)

        self._setup_style()

    def _setup_style(self):
        self.setObjectName(type(self).__name__)

    # qProperty
    # noinspection PyPep8Naming
    @Property(QColor)
    def placeholderColor(self):
        return self._placeholder_color

    # noinspection PyPep8Naming
    @placeholderColor.setter
    def placeholderColor(self, color):
        self._placeholder_color = color
        self.update()

    # noinspection PyPep8Naming
    @Property(int)
    def border_radius(self):
        return self._border_radius

    # noinspection PyPep8Naming
    @border_radius.setter
    def border_radius(self, v):
        self._border_radius = v
        self.update()

    # ---------- 过滚支持 ----------
    @property
    def bottom_overscroll(self) -> int:
        return self._bottom_overscroll

    @bottom_overscroll.setter
    def bottom_overscroll(self, value: int) -> None:
        self._bottom_overscroll = max(0, value)

    def set_scroll_y(self, y: float) -> None:
        """扩展滚动范围，允许底部过滚，以便绘制背景色。"""
        max_scroll = self._total_content_height - self.contentsRect().height()
        if max_scroll < 0:
            max_scroll = 0
        # 原顶部过滚上限继承自基类 self.overscroll_top
        clamped = max(-self.overscroll_top, min(y, max_scroll + self._bottom_overscroll))
        if clamped != self._scroll_y:
            self._scroll_y = clamped
            self.scroll_changed.emit(int(self._scroll_y))
            self.update()
            self._emit_visible_range_if_changed()

    # ---------- 重写绘制 ----------
    def paintEvent(self, event) -> None:
        """使用圆角裁剪、调色板背景和空单元格颜色进行绘制。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. 圆角裁剪
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._border_radius, self._border_radius)
        painter.setClipPath(path)

        # 2. 整体背景（Window 颜色），过滚区域自然显示此颜色
        bg_color = self.palette().color(QPalette.ColorRole.Window)
        painter.fillRect(self.rect(), bg_color)

        # 3. 正常内容绘制
        cell_sz = self._get_cell_size()
        if cell_sz <= 0:
            return

        rect = self.contentsRect()
        viewport_h = rect.height()

        # 可见行范围（与基类相同，但允许因底部过滚导致 last_row 可能超过真实总行数）
        first_row = max(0, int(self._scroll_y // cell_sz))
        bottom_y = self._scroll_y + viewport_h
        last_row = int((bottom_y + cell_sz - 1) // cell_sz)
        if self._total_rows > 0:
            last_row = min(last_row, self._total_rows - 1)
        else:
            last_row = -1

        if first_row <= last_row:
            # 空白单元格颜色：使用调色板的 Base 角色
            empty_color = self._placeholder_color

            for row in range(first_row, last_row + 1):
                for col in range(self.single_row_num):
                    idx = self._row_col_to_index(row, col)
                    x = col * cell_sz
                    y = int(row * cell_sz - self._scroll_y)
                    target_rect = QRectF(x, y, cell_sz, cell_sz).toRect()

                    if idx in self._pixmap_cache:
                        pixmap = self._pixmap_cache[idx]
                        painter.drawPixmap(target_rect, pixmap)
                    else:
                        # 空白单元格填充 Base 颜色
                        painter.fillRect(target_rect, empty_color)

    # 可选：响应调色板变化自动重绘
    def changeEvent(self, event):
        if event.type() == event.Type.PaletteChange:
            self.update()
        super().changeEvent(event)
