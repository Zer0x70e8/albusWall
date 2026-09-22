#
"""绘制层：给每个可见 item 画背景 + 缩略图。

QSS 生效靠调用 PE_PanelItemViewItem；
缩放结果通过 QPixmapCache 做全局缓存，避免重复 SmoothTransformation。
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainterPath, QPixmap, QPixmapCache
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from ..model.album_model import AlbumRole


# noinspection method-overriding
class AlbumDelegate(QStyledItemDelegate):
    """专辑缩略图 delegate。

    delegate 会被 view 复用到不同 index，因此这里不保存与 index 相关的
    状态；所有状态只跟外观配置（圆角、内边距）有关。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._radius: int = 8
        self._padding: int = 4

    # ---------- 外观配置 ----------
    def setBorderRadius(self, radius: int) -> None:
        self._radius = max(0, int(radius))

    def borderRadius(self) -> int:
        return self._radius

    def setPadding(self, padding: int) -> None:
        self._padding = max(0, int(padding))

    def padding(self) -> int:
        return self._padding

    # ---------- QStyledItemDelegate ----------
    def sizeHint(self, option, index: QModelIndex) -> QSize:
        # 真实尺寸由 view.setGridSize 决定，这里只是兜底。
        return QSize(100, 100)

    def paint(self, painter, option, index: QModelIndex) -> None:
        # 1) QSS 生效：画背景 / 边框 / 选中态
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem,
            opt, painter, widget,
        )

        # 2) 缩略图
        pixmap = index.data(AlbumRole.Pixmap)
        if pixmap is None or pixmap.isNull():
            return

        rect = option.rect.adjusted(
            self._padding, self._padding, -self._padding, -self._padding,
        )
        if rect.width() <= 0 or rect.height() <= 0:
            return

        scaled = self._scaled_cached(pixmap, rect.size())
        x = rect.x() + (rect.width() - scaled.width()) // 2
        y = rect.y() + (rect.height() - scaled.height()) // 2

        if self._radius > 0:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(rect),
                float(self._radius), float(self._radius),
            )
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(x, y, scaled)
            painter.restore()
        else:
            painter.drawPixmap(x, y, scaled)

    # ---------- 内部 ----------
    # noinspection unreachable-code
    @staticmethod
    def _scaled_cached(pixmap: QPixmap, size: QSize) -> QPixmap:
        key = f"albumThumb:{pixmap.cacheKey()}:{size.width()}x{size.height()}"
        # noinspection argument-list
        cached = QPixmapCache.find(key)
        if isinstance(cached, QPixmap):
            return cached
        scaled = pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        QPixmapCache.insert(key, scaled)
        return scaled
