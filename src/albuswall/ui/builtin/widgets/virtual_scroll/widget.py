#
""""""

from PySide6.QtCore import Qt, Signal

try:
    from .style import StyledVirtualScrollWidget
except ImportError:
    from style import StyledVirtualScrollWidget


class VirtualScrollWidget(StyledVirtualScrollWidget):
    unit_pressed = Signal(int)
    unit_right_clicked = Signal(int)

    # event
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

    #
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

    def set_column_count(self, n: int) -> None:
        n = max(1, min(12, int(n)))
        self._apply_column_count(n)


if __name__ == '__main__':
    import sys
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
    from PySide6.QtGui import QImage, QFont, QColor, QPainter, QPixmap

    app = QApplication(sys.argv)


    def image_provider(number, size=256) -> QImage:
        """
        Generate a placeholder QImage for a given number.
        This function is designed to be executed in a worker thread because it only operates on QImage,
        which is safe to use in a non-GUI thread in Qt5+ when painting on a QImage with
        QPainter (QImage is a paint device with a render target).

        Args:
            number: The numeric value to display in the centre of the image.
            size: Side length of the square image. Defaults to 256.

        Returns:
            A QImage filled with white and centred black text of the given number.
        """
        # This function runs in a worker thread; only touch QImage, no GUI widgets.
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.white)
        # Painting on a QImage that has a render target is thread-safe (Qt5+).
        # noinspection SpellCheckingInspection
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont("Arial", size // 4)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("black"))
        if isinstance(number, float):
            if number.is_integer():
                text = str(int(number))
            else:
                text = f"{number:.2f}"
        else:
            text = str(number)
        painter.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return img


    # ── 1. 顶层窗口：模拟 Window 的设置 ──
    container = QWidget()
    # container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    # 给窗口一个绿色调，方便区分"窗口背景"和"widget 背景"
    # container.setStyleSheet("QWidget#Root { background: rgba(0, 255, 0, 60); }")
    container.setStyleSheet("QWidget#Root { background: red; }")
    container.setObjectName("Root")

    grid = VirtualScrollWidget()
    grid.setObjectName("Grid")

    # # ── 2. 逐项开关，对照观察 ──
    # # 开关 A：打开样式表背景支持（默认没有，QSS 不生效）
    # grid.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    #
    # # 开关 B：直接干掉空格子的灰色占位
    # grid._placeholder_color = QColor(0, 0, 0, 0)
    #
    # 开关 C：告诉 QSS 这个控件要透明
    grid.setStyleSheet("#Grid { background: transparent; }")

    grid.set_pixmap(0, QPixmap(image_provider(0)))
    grid.set_pixmap(1, QPixmap(image_provider(1)))
    grid.set_scroll_y(0)

    layout = QVBoxLayout(container)
    layout.addWidget(grid)
    container.resize(800, 600)
    container.show()

    app.exec()
