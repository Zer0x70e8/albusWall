#
""""""

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QPainter,
    QPixmap,
    QWheelEvent,
    QNativeGestureEvent,
    QMouseEvent,
    QResizeEvent,
    QPaintEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


class ImageViewer(QWidget):
    """图片视图：缩放 + 四向平移（作为 Detail 的子控件存在）。"""

    MIN_SCALE = 0.05
    MAX_SCALE = 64.0
    WHEEL_ZOOM_STEP = 1.2
    SCROLL_STEP = 60.0
    TRACKPAD_ZOOM_SENSITIVITY = 0.005

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._dragging = False
        self._drag_pos = QPointF()

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # ------------------------------------------------------------- 数据接口
    def clear(self) -> None:
        self.set_pixmap(QPixmap())

    def reset_view(self) -> None:
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self.update()

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        """同步设置图片并复位视图（保持原语义）。"""
        self._pixmap = QPixmap() if pixmap is None else QPixmap(pixmap)
        self.reset_view()

    def replace_pixmap(self, pixmap: QPixmap | None) -> None:
        """切换资产时用：保留当前缩放/偏移，只换图。"""
        self._pixmap = QPixmap() if pixmap is None else QPixmap(pixmap)
        self._clamp_offset()
        self.update()

    # ------------------------------------------------------------- 几何
    def _fit_scale(self) -> float:
        if self._pixmap.isNull():
            return 1.0
        vw, vh = max(1, self.width()), max(1, self.height())
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw <= 0 or ph <= 0:
            return 1.0
        return min(vw / pw, vh / ph)

    def _effective_scale(self) -> float:
        return self._fit_scale() * self._scale

    def _image_rect(self) -> QRectF:
        if self._pixmap.isNull():
            return QRectF()
        s = self._effective_scale()
        w = self._pixmap.width() * s
        h = self._pixmap.height() * s
        cx = self.width() / 2.0 + self._offset.x()
        cy = self.height() / 2.0 + self._offset.y()
        return QRectF(cx - w / 2.0, cy - h / 2.0, w, h)

    def _clamp_offset(self) -> None:
        if self._pixmap.isNull():
            self._offset = QPointF(0.0, 0.0)
            return
        s = self._effective_scale()
        iw, ih = self._pixmap.width() * s, self._pixmap.height() * s
        mx = max(0.0, (iw - self.width()) / 2.0)
        my = max(0.0, (ih - self.height()) / 2.0)
        self._offset = QPointF(
            max(-mx, min(mx, self._offset.x())),
            max(-my, min(my, self._offset.y())),
        )

    # ------------------------------------------------------------- 缩放

    def _zoom_to(self, new_scale: float, anchor: QPointF) -> None:
        new_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, new_scale))
        if abs(new_scale - self._scale) < 1e-9:
            return
        old_eff = self._effective_scale()
        self._scale = new_scale
        new_eff = self._effective_scale()
        if old_eff > 0.0:
            ratio = new_eff / old_eff
            center = QPointF(self.width() / 2.0, self.height() / 2.0)
            delta = anchor - center - self._offset
            self._offset = self._offset + delta * (1.0 - ratio)
        self._clamp_offset()
        self.update()

    # ------------------------------------------------------------- 事件

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._pixmap.isNull():
            event.ignore()
            return

        pixel = event.pixelDelta()
        angle = event.angleDelta()

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if not pixel.isNull():
                factor = math.exp(pixel.y() * self.TRACKPAD_ZOOM_SENSITIVITY)
            else:
                factor = self.WHEEL_ZOOM_STEP ** (angle.y() / 120.0)
            if abs(factor - 1.0) > 1e-6:
                self._zoom_to(self._scale * factor, event.position())
            event.accept()
            return

        if not pixel.isNull():
            dx, dy = float(pixel.x()), float(pixel.y())
        else:
            dx = angle.x() / 120.0 * self.SCROLL_STEP
            dy = angle.y() / 120.0 * self.SCROLL_STEP

        if dx or dy:
            self._offset -= QPointF(dx, dy)
            self._clamp_offset()
            self.update()
        event.accept()

    def event(self, event: QEvent) -> bool:
        if isinstance(event, QNativeGestureEvent):
            g = event.gestureType()

            if g == Qt.NativeGestureType.ZoomNativeGesture:
                factor = 1.0 + event.value()
                if factor > 0.0:
                    self._zoom_to(self._scale * factor, event.position())
                return True

            if g == Qt.NativeGestureType.PanNativeGesture:
                d = event.delta()
                self._offset += QPointF(d.x(), d.y())
                self._clamp_offset()
                self.update()
                return True

        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
                event.button() in (
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.MiddleButton,
        )
                and not self._pixmap.isNull()
        ):
            self._dragging = True
            self._drag_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            delta = event.position() - self._drag_pos
            self._drag_pos = event.position()
            self._offset += delta
            self._clamp_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.button() in (
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.MiddleButton,
        ):
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._pixmap.isNull():
            return super().mouseDoubleClickEvent(event)

        if abs(self._scale - 1.0) > 1e-3:
            self.reset_view()
        else:
            original = 1.0 / max(self._fit_scale(), 1e-6)
            self._zoom_to(original, event.position())

        return event.accept()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._clamp_offset()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._pixmap.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(
            self._image_rect(),
            self._pixmap,
            QRectF(self._pixmap.rect()),
        )
