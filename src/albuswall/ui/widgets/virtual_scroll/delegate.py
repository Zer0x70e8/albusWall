#
""""""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap


@runtime_checkable
class PixmapCacheProtocol(Protocol):
    """widget 对缓存的最小要求。

    任何满足以下行为的对象都可以作为 ``pixmap_cache`` 注入：
    - ``cache[i] = pm``      存入
    - ``pm = cache[i]``      取出（缺失时应抛 ``KeyError``）
    - ``i in cache``         探测
    - ``pm = cache.get(i)``  取出，缺失返回 ``None``
    - ``cache.clear()``      清空
    """

    def __getitem__(self, key: int) -> QPixmap: ...

    def __setitem__(self, key: int, value: QPixmap) -> None: ...

    def __contains__(self, key: object) -> bool: ...

    def get(self, key: int, default: Optional[QPixmap] = None) -> Optional[QPixmap]: ...

    def clear(self) -> None: ...


@dataclass
class CellOption:
    """单元格绘制所需的上下文信息。

    Attributes:
        index:     该项在数据源中的全局索引。
        rect:      单元格在 widget 坐标系下的目标矩形。
        cell_size: 单元格边长（像素），等于 rect.width() == rect.height()。
        selected:  是否处于选中状态（预留扩展）。
        hovered:   是否处于鼠标悬停状态（预留扩展）。
        focused:   是否获得键盘焦点（预留扩展）。
    """
    index: int
    rect: QRect
    cell_size: int
    selected: bool = False
    hovered: bool = False
    focused: bool = False


class CellDelegate:
    """单元格绘制委托的抽象基类。

    通过替换 ``VirtualScrollWidget.delegate``，可以在不改动滚动、缓存、
    命中测试等核心逻辑的前提下，自定义单元格的绘制方式。

    子类只需实现 :meth:`paint`。
    """

    def paint(self, painter: QPainter, option: CellOption) -> None:
        """在 ``option.rect`` 处绘制 ``option.index`` 对应的单元格。

        Args:
            painter: 已完成坐标变换的 ``QPainter``，坐标系为 widget 本地坐标系。
            option:  单元格的几何与状态信息。
        """
        raise NotImplementedError


class PixmapCellDelegate(CellDelegate):
    """默认委托：从 getter 获取 ``QPixmap`` 并绘制。

    找不到 pixmap 时：
    - 若设置了 ``placeholder_color``，则填充该颜色；
    - 否则什么都不画（背景由 widget 自身决定）。

    Args:
        pixmap_getter:     接收全局索引，返回 ``QPixmap`` 或 ``None``。
        placeholder_color: 空白单元格的填充色；``None`` 表示不填充。
        border_radius:     单元格圆角半径（像素）；0 表示直角。
        smooth:            是否开启平滑缩放（仅作为提示，由 widget 决定）。
    """

    def __init__(
            self,
            pixmap_getter: Callable[[int], Optional[QPixmap]],
            *,
            placeholder_color: Optional[QColor] = None,
            border_radius: int = 0,
            smooth: bool = True,
    ) -> None:
        self._get_pixmap = pixmap_getter
        self._placeholder_color = placeholder_color
        self._border_radius = max(0, int(border_radius))
        self._smooth = smooth

    # ---- 属性 ------------------------------------------------------------

    @property
    def placeholder_color(self) -> Optional[QColor]:
        return self._placeholder_color

    @placeholder_color.setter
    def placeholder_color(self, color: Optional[QColor]) -> None:
        self._placeholder_color = color

    @property
    def border_radius(self) -> int:
        return self._border_radius

    @border_radius.setter
    def border_radius(self, value: int) -> None:
        self._border_radius = max(0, int(value))

    @property
    def smooth(self) -> bool:
        return self._smooth

    @smooth.setter
    def smooth(self, value: bool) -> None:
        self._smooth = bool(value)

    # ---- paint -----------------------------------------------------------

    def paint(self, painter: QPainter, option: CellOption) -> None:
        pixmap = self._get_pixmap(option.index)
        if pixmap is None or pixmap.isNull():
            if self._placeholder_color is not None:
                painter.fillRect(option.rect, self._placeholder_color)
            return

        if self._border_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(
                option.rect, self._border_radius, self._border_radius
            )
            painter.save()
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
            painter.drawPixmap(option.rect, pixmap)
            painter.restore()
        else:
            painter.drawPixmap(option.rect, pixmap)
