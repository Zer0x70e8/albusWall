#
"""
BlurLabel – a QLabel that renders a live blurred snapshot of a target widgets as its background.
Only the area occupied by the label is blurred.
"""

import sys
from typing import Optional

from PySide6.QtCore import (
    QEvent, QTimer, Qt, QRect, QObject,
    Property, QPoint, QRectF, Signal)
from PySide6.QtWidgets import (
    QApplication, QMainWindow,
    QWidget, QLabel,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect
)
from PySide6.QtGui import (
    QPixmap, QPainter,
    QShowEvent, QPaintEvent, QResizeEvent, QCloseEvent, QImage#, QRegion
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
    blurRadiusChanged = Signal(float)

    def __init__(self, *args, target: Optional[QWidget] = None,
                 blur_radius: float = 15,
                 draw_label_content=DRAW_LABEL_CONTENT, **kwargs):
        super().__init__(*args, **kwargs)
        self.setObjectName(type(self).__name__)

        self._target = target
        self._blur_radius = float(blur_radius)
        self._blurred_pixmap: Optional[QPixmap] = None
        self._updating = False
        self._draw_label_content = draw_label_content

        # Frameless sub-window that stays boot top of the target
        if self.parent() is None:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        # self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # self.setStyleSheet("background: transparent;")
        if self._draw_label_content:
            self.setStyleSheet(
                f"#{self.objectName()} {{ background: transparent; }}")
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAutoFillBackground(False)

        if self._target:
            self._target.installEventFilter(self)

        # Debounced timer to avoid excessive blur recalculations
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(100)
        self._update_timer.timeout.connect(self._update_blur)

    @Property(float)
    def blur_radius(self) -> float:
        return self._blur_radius

    @blur_radius.setter
    def blur_radius(self, v: float) -> None:
        self._set_blur_radius(v)

    def _set_blur_radius(self, radius: float) -> None:
        radius = float(radius)
        if radius == self._blur_radius:
            return
        self._blur_radius = radius
        self.blurRadiusChanged.emit(radius)
        self._update_blur()

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
        self._update_timer.start()

    def _update_blur(self) -> None:
        """Capture the target widgets and generate a blurred background."""
        # if not self._target or not self._target.isVisible():
        #     self._updating = False
        #     return
        # if self.window() is not self._target.window():
        #     self._updating = False
        #     return
        if self._updating:
            return
        self._updating = True
        #
        # if (not self._target
        #         or not self._target.isVisible()
        #         or self.window() is None
        #         or self.window() is not self._target.window()):
        #     self._updating = False
        #     return

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

        # pixmap = QPixmap(self._target.size())
        # pixmap.fill(Qt.GlobalColor.transparent)
        # # noinspection unsupported-operator
        # self._target.render(
        #     pixmap,
        #     QPoint(0, 0),
        #     QRegion(),  # 全区域
        #     QWidget.RenderFlag.DrawWindowBackground
        #     | QWidget.RenderFlag.DrawChildren,
        # )
        # grabbed = pixmap
        #
        # if was_visible:
        #     self.setVisible(True)
        #
        # if grabbed and not grabbed.isNull():
        #     self._blurred_pixmap = self._apply_blur(grabbed, self._blur_radius)
        # else:
        #     self._blurred_pixmap = None
        #
        # self.update()
        # self._updating = False

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
        # if self._blurred_pixmap and self._target:
        #     painter = QPainter(self)
        #     # Calculate where this label sits inside the target's coordinate system
        #     # (global → target mapping)
        #     label_global_pos = self.mapToGlobal(QPoint(0, 0))
        #     label_in_target = self._target.mapFromGlobal(label_global_pos)
        #
        #     # Source rectangle boot the full blurred pixmap
        #     source_rect = QRect(label_in_target, self.size())
        #
        #     # Draw the matching piece of the blurred pixmap scaled into our rect
        #     painter.drawPixmap(self.rect(), self._blurred_pixmap, source_rect)
        #     painter.end()

        if self._blurred_pixmap and self._target:
            painter = QPainter(self)
            # 用全局坐标差求 label 在 target 坐标系里的位置
            label_gp = self.mapToGlobal(QPoint(0, 0))
            target_gp = self._target.mapToGlobal(QPoint(0, 0))
            label_in_target = label_gp - target_gp  # QPoint - QPoint

            source_rect = QRect(label_in_target, self.size())
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
        """Clean up the installed event filter boot the target."""
        if self._target:
            self._target.removeEventFilter(self)
        super().closeEvent(event)


if __name__ == "__main__":
    from PySide6.QtGui import QColor, QLinearGradient, QPen, QFont

    class BackgroundWidget(QWidget):
        """画一个彩色渐变 + 网格 + 大字的背景，让模糊效果清晰可见。"""

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # 1. 渐变底色
            grad = QLinearGradient(0, 0, self.width(), self.height())
            grad.setColorAt(0.0, QColor("#FF6B6B"))
            grad.setColorAt(0.5, QColor("#4ECDC4"))
            grad.setColorAt(1.0, QColor("#FFE66D"))
            p.fillRect(self.rect(), grad)

            # 2. 网格线
            p.setPen(QPen(QColor(255, 255, 255, 110), 2))
            step = 40
            for x in range(0, self.width(), step):
                p.drawLine(x, 0, x, self.height())
            for y in range(0, self.height(), step):
                p.drawLine(0, y, self.width(), y)

            # 3. 大标题
            p.setPen(QColor(30, 30, 30))
            font = QFont()
            font.setPointSize(36)
            font.setBold(True)
            p.setFont(font)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "FROSTED GLASS")
            p.end()


    # noinspection PyProtectedMember
    class MainWindow(QMainWindow):
        """两个模糊程度不同的 BlurLabel，直接叠在有内容的背景上。"""

        def __init__(self):
            super().__init__()
            self.setWindowTitle("BlurLabel – Two Local Blurs")
            self.setGeometry(100, 100, 720, 460)

            central = BackgroundWidget()
            self.setCentralWidget(central)

            # 两个 BlurLabel：模糊半径不同，位置错开并排显示
            self._label_light = self._make_blur_label(
                central, "<b>Light Blur (4px)</b>", 4.0, "red", (60, 90)
            )
            self._label_heavy = self._make_blur_label(
                central, "<b>Heavy Blur (30px)</b>", 30.0, "blue", (420, 240)
            )

        def _make_blur_label(self, parent, text, radius, color, pos):
            """创建一个固定尺寸、指定模糊半径的 BlurLabel。"""
            label = BlurLabel(
                target=self,
                text=text,
                blur_radius=radius,
                parent=parent,
            )
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(
                f"color: {color}; font-size: 20px; padding: 20px;"
                "background: transparent;"
            )
            label.setFixedSize(220, 100)
            label.move(*pos)
            label.show()
            label.raise_()
            return label

        def showEvent(self, event):
            """窗口显示后，延迟触发一次模糊刷新，确保拿到完整窗口快照。"""
            super().showEvent(event)
            if hasattr(self, "_label_light"):
                QTimer.singleShot(0, self._label_light._update_timer.start)
                QTimer.singleShot(0, self._label_heavy._update_timer.start)


    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
