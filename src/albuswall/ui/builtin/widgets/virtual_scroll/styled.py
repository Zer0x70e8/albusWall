#
""""""

from typing import Optional

from PySide6.QtCore import QRectF, Property, QEvent
from PySide6.QtGui import QPainter, QPainterPath, QPalette, QColor
from PySide6.QtWidgets import QWidget

from .basic import VirtualScrollWidget
from .delegate import CellDelegate, PixmapCellDelegate, PixmapCacheProtocol


class StyledVirtualScrollWidget(VirtualScrollWidget):
    """支持圆角与调色板背景的虚拟滚动组件。

    额外特性：
    - border_radius: 控件圆角半径（像素），0 表示直角。
    - placeholderColor: 空单元格填充色，setter 会自动同步到默认 delegate。
    - 使用 QPalette 的 Window 角色填充整体背景（含过滚区域）。
    """

    def __init__(
            self,
            parent: Optional[QWidget] = None,
            *,
            pixmap_cache: Optional[PixmapCacheProtocol] = None,
    ):
        super().__init__(parent, pixmap_cache=pixmap_cache)
        self._border_radius = 0
        self._placeholder_color = QColor(0, 0, 0, 0)
        self._apply_delegate_style()
        self._setup_style()

    # ---- 初始化 ----------------------------------------------------------

    def _setup_style(self) -> None:
        self.setObjectName(type(self).__name__)

    def _apply_delegate_style(self) -> None:
        if isinstance(self._delegate, PixmapCellDelegate):
            self._delegate.placeholder_color = self._placeholder_color

    # ---- delegate：替换时同步样式 -----------------------------------------

    @property
    def delegate(self) -> CellDelegate:
        return self._delegate

    @delegate.setter
    def delegate(self, value: CellDelegate) -> None:
        self._delegate = value
        self._apply_delegate_style()
        self.update()

    # ---- qProperty -------------------------------------------------------

    # noinspection PyPep8Naming
    @Property(QColor)
    def placeholderColor(self) -> QColor:
        return self._placeholder_color

    # noinspection PyPep8Naming
    @placeholderColor.setter
    def placeholderColor(self, color: QColor) -> None:
        self._placeholder_color = color
        if isinstance(self._delegate, PixmapCellDelegate):
            self._delegate.placeholder_color = color
        self.update()

    # noinspection PyPep8Naming
    @Property(int)
    def border_radius(self) -> int:
        return self._border_radius

    # noinspection PyPep8Naming
    @border_radius.setter
    def border_radius(self, v: int) -> None:
        self._border_radius = int(v)
        self.update()

    # ---- 绘制：仅覆盖背景钩子 --------------------------------------------

    def _paint_background(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 圆角裁剪：作用于后续全部单元格绘制
        if self._border_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(self.rect()),
                self._border_radius,
                self._border_radius,
            )
            painter.setClipPath(path)

        # 整体背景（Window 角色），过滚区域自然显示此颜色
        bg_color = self.palette().color(QPalette.ColorRole.Window)
        painter.fillRect(self.rect(), bg_color)

    # ---- 调色板变化自动重绘 ----------------------------------------------

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)
