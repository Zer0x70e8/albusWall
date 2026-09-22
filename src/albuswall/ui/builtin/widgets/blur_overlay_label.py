#
"""
BlurLabel – a QLabel that renders a live blurred snapshot of a target widgets as its background.
Only the area occupied by the label is blurred.
"""

import sys
from typing import Optional

from PySide6.QtCore import QEvent, QTimer, Qt, QRect, QObject, Property, QPoint, QRectF
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton,
    QVBoxLayout, QWidget, QLabel,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect
)
from PySide6.QtGui import (
    QPixmap, QPainter,
    QShowEvent, QPaintEvent, QResizeEvent, QCloseEvent, QImage
)

DRAW_LABEL_CONTENT = True


class BlurLabel(QLabel):
    """
    A label with a real-time frosted-glass blur effect.

    The label shows a blurred version of the underlying *target* widgets,
    but **only within its own bounding rectangle**.  The rest of the target
    remains perfectly visible.

    :param target:      The widgets whose appearance is blurred.
    :param text:        Label text (same as QLabel).
    :param blur_radius: Blur radius (default 15).
    :param parent:      Parent widgets (required so the label acts as a SubWindow).
    """

    def __init__(self, *args, target: Optional[QWidget] = None,
                 blur_radius: float = 15,
                 draw_label_content=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.setObjectName(type(self).__name__)

        self._target = target
        self._blur_radius = blur_radius
        self._blurred_pixmap: Optional[QPixmap] = None
        self._updating = False
        self._draw_label_content = DRAW_LABEL_CONTENT

        # Frameless sub-window that stays on top of the target
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.SubWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # self.setStyleSheet("background: transparent;")
        if self._draw_label_content and draw_label_content:
            self.setStyleSheet("background: transparent;")

        if self._target:
            self._target.installEventFilter(self)

        # Debounced timer to avoid excessive blur recalculations
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._update_blur)

    @Property(float)
    def blur_radius(self) -> float:
        """Current blur radius."""
        return self._blur_radius

    @blur_radius.setter
    def blur_radius(self, radius: float) -> None:
        """Change the blur radius and refresh the background immediately."""
        self._blur_radius = radius
        self._update_blur()

    @property
    def target_widget(self):
        return self._target

    @target_widget.setter
    def target_widget(self, widget: QWidget | None):
        if self._target:
            self._target.removeEventFilter(self)
        self._target = widget
        if widget:
            widget.installEventFilter(self)
        self._update_blur()

    def _update_blur(self) -> None:
        """Capture the target widgets and generate a blurred background."""
        if self._updating:
            return
        self._updating = True

        if not self._target or not self._target.isVisible():
            self._updating = False
            return

        # Temporarily hide ourselves so we don't appear in the snapshot
        was_visible = self.isVisible()
        if was_visible:
            self.setVisible(False)

        grabbed = self._target.grab()
        pixmap = grabbed if isinstance(grabbed, QPixmap) and not grabbed.isNull() else None

        if was_visible:
            self.setVisible(True)

        if pixmap:
            self._blurred_pixmap = self._apply_blur(pixmap, self._blur_radius)
        else:
            self._blurred_pixmap = None

        self.update()
        self._updating = False

    @staticmethod
    def _apply_blur(pixmap: QPixmap, radius: float) -> QPixmap:
        """Apply a Gaussian blur to *pixmap* and return the blurred result."""
        image = pixmap.toImage().convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied
        )
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(QPixmap.fromImage(image))
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(radius)
        # 关键：不要让效果把边缘向外扩张到 sceneRect 之外
        effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(effect)
        scene.addItem(item)

        out = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # 用明确的源矩形，且和输出尺寸严格对应
        src = QRectF(item.boundingRect())
        dst = QRectF(0, 0, image.width(), image.height())
        scene.render(painter, dst, src, Qt.AspectRatioMode.IgnoreAspectRatio)
        painter.end()
        return QPixmap.fromImage(out)

    # ---------- Event overrides (modified) ----------

    def showEvent(self, event: QShowEvent) -> None:
        """Update the blur when shown. No longer forces geometry to match target."""
        self._update_blur()
        super().showEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        """
        Draw the blurred pixmap only for the region that corresponds to
        this label's position over the target widgets.
        """
        if self._blurred_pixmap and self._target:
            painter = QPainter(self)
            # Calculate where this label sits inside the target's coordinate system
            # (global → target mapping)
            label_global_pos = self.mapToGlobal(QPoint(0, 0))
            label_in_target = self._target.mapFromGlobal(label_global_pos)

            # Source rectangle on the full blurred pixmap
            source_rect = QRect(label_in_target, self.size())

            # Draw the matching piece of the blurred pixmap scaled into our rect
            painter.drawPixmap(self.rect(), self._blurred_pixmap, source_rect)
            painter.end()

        if self._draw_label_content:
            super().paintEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Debounce blur updates when the widgets is resized."""
        self._update_timer.start()
        super().resizeEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """
        Keep the blur updated when the target moves or resizes,
        but do **not** force the label's geometry to match the target anymore.
        """
        if obj is self._target:
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self._update_timer.start()
        return super().eventFilter(obj, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Clean up the installed event filter on the target."""
        if self._target:
            self._target.removeEventFilter(self)
        super().closeEvent(event)


if __name__ == "__main__":
    class MainWindow(QMainWindow):
        """Example window that demonstrates the updated BlurLabel."""

        def __init__(self):
            super().__init__()
            self.setWindowTitle("BlurLabel – Local Blur")
            self.setGeometry(100, 100, 600, 400)

            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)

            btn = QPushButton("Show Blur Label")
            btn.clicked.connect(self.show_blur_label)
            layout.addWidget(btn)

            for i in range(5):
                layout.addWidget(QPushButton(f"Button {i + 1}"))

            self._blur_label: Optional[BlurLabel] = None

        def show_blur_label(self) -> None:
            """Create (once) and show a small, movable BlurLabel overlay."""
            if self._blur_label is None:
                label = BlurLabel(
                    target=self,
                    text="<b>Frosted Glass</b>",
                    blur_radius=12,
                    parent=self
                )
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setStyleSheet(
                    "color: red; font-size: 20px; padding: 20px;"
                    "background: transparent;"
                )
                # Let the label size itself around the text
                label.adjustSize()
                self._blur_label = label

            # Position the label at a fixed offset inside the window (just for demo)
            if self._blur_label:
                self._blur_label.move(50, 150)
                self._blur_label.show()


    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
