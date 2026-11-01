#
"""公共委托基类：把一张 ``QPixmap`` 画成正方形格子。

相册封面、资产格子、导航缩略图长得一样（居中裁剪的正方形图 + 可选角标，
如收藏星标、倒计时）。这些行为收敛在本类里，子类只覆写几个钩子：

========================  ==============================================
钩子                      默认行为
========================  ==============================================
:meth:`_draw_frame`       什么都不画（留给需要描边/底色的皮肤）
:meth:`_draw_placeholder` 什么都不画（无图时占位）
:meth:`_draw_content`     居中裁剪铺满（正方形）
:meth:`_draw_overlay`     什么都不画（标题 / 描述 / 角标从这里进）
:meth:`_draw_selection`   什么都不画（选中态默认交给 QSS 背景）
========================  ==============================================

复用工具：:meth:`crop_square`（把任意图裁成 ``side×side``）。

与旧版的差别
------------
1. QSS 背景**总是**先画（旧版仅在无图时画，导致有图的格子没有 hover 效果）；
2. 旧版声明了 ``_draw_content`` / ``_draw_selection`` 钩子却从没调用，
   这里补上；
3. 内边距 / 圆角变成委托自己的属性（旧版散落在两个委托里），
   底层的 ``AlbumDelegate`` / ``AlbumCoverDelegate`` 因此可以合二为一。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

__all__ = ["SquareThumbDelegate"]


class SquareThumbDelegate(QStyledItemDelegate):
    """把装饰图按 ``KeepAspectRatioByExpanding`` 缩放后居中裁剪成正方形。"""

    def __init__(
            self,
            parent: Any = None,
            *,
            decoration_role: int = int(Qt.ItemDataRole.DecorationRole),
            default_size: QSize | None = None,
            padding: int = 0,
            border_radius: int = 0,
    ) -> None:
        super().__init__(parent)
        self._decoration_role = int(decoration_role)
        self._default_size = QSize(default_size or QSize(96, 96))
        self._padding = max(0, int(padding))
        self._border_radius = max(0, int(border_radius))

    # ------------------------------------------------------------------ #
    # 外观配置
    # ------------------------------------------------------------------ #
    def set_decoration_role(self, role: int) -> None:
        """改从哪个 role 取图（默认 ``DecorationRole``）。"""
        self._decoration_role = int(role)

    def decoration_role(self) -> int:
        return self._decoration_role

    def set_default_size_hint(self, size: QSize) -> None:
        """设置 ``sizeHint``；仅在 view 没设 ``gridSize`` 时生效。"""
        self._default_size = QSize(size)

    def set_padding(self, padding: int) -> None:
        """格子四周留白（像素）。"""
        self._padding = max(0, int(padding))

    def padding(self) -> int:
        return self._padding

    def set_border_radius(self, radius: int) -> None:
        """缩略图圆角半径（像素）；0 表示直角。"""
        self._border_radius = max(0, int(radius))

    def border_radius(self) -> int:
        return self._border_radius

    # ------------------------------------------------------------------ #
    # QStyledItemDelegate
    # ------------------------------------------------------------------ #
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        view = option.widget
        if isinstance(view, QListView):
            grid = view.gridSize()
            if grid.isValid() and not grid.isEmpty():
                return grid
        return QSize(self._default_size)

    def paint(self, painter: QPainter, option, index) -> None:
        # 整体 save/restore：钩子里的字体 / 画笔改动不会污染后续格子的绘制
        painter.save()
        try:
            # 1) 背景 / 边框 / 选中态：走 QSS，永远不会被自绘遮掉
            self._draw_background(painter, option, index)

            target = self.content_rect(option.rect)
            if target.isEmpty():
                return

            pixmap = index.data(self._decoration_role)
            has_pixmap = isinstance(pixmap, QPixmap) and not pixmap.isNull()
            if has_pixmap:
                self._draw_frame(painter, target, option)
                self._draw_content(painter, target, pixmap, option, index)
            else:
                self._draw_placeholder(painter, target, option, index)

            self._draw_overlay(painter, target, option, index)
            self._draw_selection(painter, target, option, index)
        finally:
            painter.restore()

    # ------------------------------------------------------------------ #
    # 几何
    # ------------------------------------------------------------------ #
    def content_rect(self, rect: QRect) -> QRect:
        """内容区矩形（扣掉内边距）；窄到没法画时返回空矩形。"""
        rect = rect.adjusted(
            self._padding, self._padding, -self._padding, -self._padding)
        if rect.width() <= 0 or rect.height() <= 0:
            return QRect()
        return rect

    # ------------------------------------------------------------------ #
    # 绘制步骤
    # ------------------------------------------------------------------ #
    def _draw_background(self, painter: QPainter, option, index) -> None:
        """用默认样式画 item 背景，让 QSS 的选中 / hover / 交替行生效。"""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()

        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)

    def _draw_frame(self, painter: QPainter, target: QRect, option) -> None:
        """内容底框（默认不画）。"""

    def _draw_content(
            self,
            painter: QPainter,
            target: QRect,
            pixmap: QPixmap,
            option,  # noqa: ARG002 —— 钩子签名统一
            index,  # noqa: ARG002
    ) -> None:
        """默认实现：居中裁剪铺满正方形内容区。"""
        self.draw_cover(painter, target, pixmap, radius=self._border_radius)

    def _draw_placeholder(
            self,
            painter: QPainter,  # noqa: ARG002
            target: QRect,  # noqa: ARG002
            option,  # noqa: ARG002
            index,  # noqa: ARG002
    ) -> None:
        """无图时的占位（默认不画，背景留给 QSS）。"""

    def _draw_overlay(
            self,
            painter: QPainter,  # noqa: ARG002
            target: QRect,  # noqa: ARG002
            option,  # noqa: ARG002
            index,  # noqa: ARG002
    ) -> None:
        """内容之上的叠加层（标题 / 描述 / 角标从这里进）。"""

    def _draw_selection(
            self,
            painter: QPainter,  # noqa: ARG002
            target: QRect,  # noqa: ARG002
            option,  # noqa: ARG002
            index,  # noqa: ARG002
    ) -> None:
        """选中态（默认交给 QSS 背景，这里不额外描边）。"""

    # ------------------------------------------------------------------ #
    # 绘制工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def draw_cover(
            painter: QPainter,
            target: QRect,
            pixmap: QPixmap,
            *,
            radius: int = 0,
    ) -> None:
        """把 ``pixmap`` 按「铺满 + 居中裁剪」画进 ``target``。

        先用 ``KeepAspectRatioByExpanding`` 放大到不小于目标，再取中心区域
        作为源矩形 —— 这样永远不变形，也永远不留边。
        """
        if pixmap.isNull() or target.isEmpty():
            return

        scaled = pixmap.scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.isNull():
            return

        source = QRect(
            max(0, (scaled.width() - target.width()) // 2),
            max(0, (scaled.height() - target.height()) // 2),
            min(target.width(), scaled.width()),
            min(target.height(), scaled.height()),
        )

        painter.save()
        if radius > 0:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(target), float(radius), float(radius))
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        else:
            painter.setClipRect(target)
        painter.drawPixmap(target, scaled, source)
        painter.restore()

    @staticmethod
    def crop_square(pixmap: QPixmap, side: int) -> QPixmap:
        """把 ``pixmap`` 裁成 ``side×side`` 正方形（先居中裁、再缩放）。

        输出尺寸严格等于 ``side×side``，不受浮点取整影响。
        """
        if side <= 0 or pixmap.isNull():
            return QPixmap()

        width, height = pixmap.width(), pixmap.height()
        if width <= 0 or height <= 0:
            return QPixmap()

        edge = min(width, height)
        square = pixmap.copy((width - edge) // 2, (height - edge) // 2, edge, edge)
        if square.width() == side and square.height() == side:
            return square
        return square.scaled(
            side,
            side,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # ------------------------------------------------------------------ #
    # 角标工具（子类共用）
    # ------------------------------------------------------------------ #
    @staticmethod
    def draw_badge(
            painter: QPainter,
            target: QRect,
            text: str,
            *,
            font=None,
            background: QColor | None = None,
            foreground: QColor | None = None,
            margin: int = 4,
    ) -> None:
        """在格子右下角画一个小药丸角标（文本式，图标也行）。"""
        if not text:
            return
        painter.save()
        if font is not None:
            painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        height = metrics.height() + 2
        rect = QRect(
            target.right() - width - margin,
            target.bottom() - height - margin,
            width,
            height,
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background or QColor(0, 0, 0, 150))
        painter.drawRoundedRect(rect, height / 2.0, height / 2.0)
        painter.setPen(foreground or QColor(255, 255, 255, 235))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
