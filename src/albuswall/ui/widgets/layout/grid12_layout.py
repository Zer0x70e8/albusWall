# -*- coding: utf-8 -*-
# noinspection SpellCheckingInspection
"""12 栅格布局（``Grid12Layout``）。

栅格划分方式与浏览器栅格系统一致：把可用宽度等分为 12 列，
控件通过 ``(row, col, colspan)`` 定位。

术语约定：
    row     —— 行号，从 0 开始、连续编号
    col     —— 起始列，0 ~ 11
    colspan —— 跨列数，>= 1

- 常规控件通过 :meth:`addWidget` 添加；
- 非 widget item（spacer / 嵌套 layout）通过 :meth:`addItem` 添加，
  以 ``row = -1`` 标记，只登记不参与布局。
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget, QWidgetItem


# noinspection SpellCheckingInspection
class _Entry(NamedTuple):
    """布局中的一条记录（row = -1 表示非 widget item）。"""
    widget_item: QWidgetItem
    row: int
    col: int
    colspan: int


# noinspection SpellCheckingInspection
class Grid12Layout(QLayout):
    """把可用宽度等分为 12 列的布局。

    控件通过 (row, col, colspan) 定位；每一列的宽度都是
    (总宽度 - 11 * 列间距) / 12，跨列控件的宽度为各列宽度之和加上中间间距，
    与浏览器栅格系统的行为一致。

    - 常规控件通过 :meth:`addWidget` 添加；
    - 非 widget item（spacer / 嵌套 layout）通过 :meth:`addItem` 添加，
      以 ``row = -1`` 标记，只登记不参与布局。

    非 Qt 覆盖方法统一使用 PEP8 snake_case
    （``detach_widget`` / ``remove_row`` / ``set_row``）。
    """

    COLS = 12

    def __init__(self, parent=None, h_spacing: int = 8, v_spacing: int = 8):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list[_Entry] = []
        self.setContentsMargins(0, 0, 0, 0)

    # ------------------------------------------------------------------
    # 添加
    # ------------------------------------------------------------------
    # noinspection PyMethodOverriding
    def addWidget(
            self,
            widget: QWidget,
            row: int,
            col: int,
            colspan: int = 1,
            alignment: Qt.AlignmentFlag = Qt.AlignmentFlag(0),
    ) -> None:
        if not isinstance(widget, QWidget):
            raise TypeError("widget 必须是 QWidget 实例")
        if row < 0:
            raise ValueError("row 必须 >= 0")
        if colspan < 1:
            raise ValueError("colspan 必须 >= 1")
        if col < 0 or col + colspan > self.COLS:
            raise ValueError(f"列位置必须在 0 ~ {self.COLS} 之间")

        self.addChildWidget(widget)
        item = QWidgetItem(widget)
        item.setAlignment(alignment)
        self._items.append(_Entry(item, row, col, colspan))
        self.invalidate()

    def addItem(self, item: QLayoutItem) -> None:
        # 非 widget item 只登记，不参与布局
        widget = item.widget()
        if widget is not None:
            self.addChildWidget(widget)
        self._items.append(_Entry(item, -1, 0, self.COLS))  # type: ignore[arg-type]
        self.invalidate()

    # ------------------------------------------------------------------
    # 移除 / 调整（非 Qt 覆盖方法，遵循 PEP8 snake_case）
    # ------------------------------------------------------------------
    def detach_widget(self, widget: QWidget) -> bool:
        """从布局中移除指定控件（不销毁控件本身）。

        :return: 找到并移除时返回 True
        """
        if not isinstance(widget, QWidget):
            raise TypeError("widget 必须是 QWidget 实例")
        for i, entry in enumerate(self._items):
            if entry.widget_item.widget() is widget:
                self._items.pop(i)
                self.invalidate()
                return True
        return False

    def remove_row(self, row: int) -> list[QWidget]:
        """移除指定行的所有控件，并让后续行号依次减 1（保持行号连续）。

        只从布局中移除，不销毁控件；调用方负责 ``setParent(None)`` /
        ``deleteLater()`` 等后续处理。

        :return: 被移除的 widget 列表（可能为空）
        """
        if row < 0:
            raise ValueError("row 必须 >= 0")

        removed: list[QWidget] = []
        remaining: list[_Entry] = []
        for entry in self._items:
            if entry.row == row:
                widget = entry.widget_item.widget()
                if widget is not None:
                    removed.append(widget)
            else:
                new_row = entry.row - 1 if entry.row > row else entry.row
                remaining.append(
                    _Entry(entry.widget_item, new_row, entry.col, entry.colspan)
                )

        self._items = remaining
        self.invalidate()
        return removed

    def set_row(self, widget: QWidget, row: int) -> bool:
        """修改某个已登记控件所在的行号。

        :return: 找到并修改时返回 True
        """
        if not isinstance(widget, QWidget):
            raise TypeError("widget 必须是 QWidget 实例")
        if row < 0:
            raise ValueError("row 必须 >= 0")
        for i, entry in enumerate(self._items):
            if entry.widget_item.widget() is widget:
                self._items[i] = _Entry(
                    entry.widget_item, row, entry.col, entry.colspan
                )
                self.invalidate()
                return True
        return False

    # ------------------------------------------------------------------
    # QLayout 接口
    # ------------------------------------------------------------------
    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items[index].widget_item
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            entry = self._items.pop(index)
            self.invalidate()
            return entry.widget_item
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    # ------------------------------------------------------------------
    # sizeHint / minimumSize 使用各自的 width 函数
    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:
        return self._measure(lambda item: item.sizeHint())

    def minimumSize(self) -> QSize:
        return self._measure(lambda item: item.minimumSize())

    def _measure(self, width_of) -> QSize:
        n = self.COLS
        hs, vs = self._h_spacing, self._v_spacing
        col_min = [0] * n
        row_max: dict[int, int] = {}

        for entry in self._items:
            # 跳过非 widget item 与隐藏控件
            if entry.row < 0 or entry.widget_item.isEmpty():
                continue

            span = max(1, min(entry.colspan, n - entry.col))
            size = width_of(entry.widget_item)
            # 跨 span 列控件宽度 = span*col_w + (span-1)*hs >= size.width()
            # ⇒ col_w >= (size.width() - (span-1)*hs) / span
            per = max(0, (size.width() - (span - 1) * hs + span - 1) // span)
            for c in range(entry.col, entry.col + span):
                col_min[c] = max(col_min[c], per)
            row_max[entry.row] = max(row_max.get(entry.row, 0), size.height())

        # 12 列等宽：列宽取所有列要求的上界，才能保证任意跨列控件满足约束
        col_w = max(col_min) if col_min else 0
        w = n * col_w + hs * (n - 1)
        h = sum(row_max.values()) + vs * max(0, len(row_max) - 1)

        margins = self.contentsMargins()
        return QSize(
            w + margins.left() + margins.right(),
            h + margins.top() + margins.bottom(),
        )

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        n = self.COLS
        hs, vs = self._h_spacing, self._v_spacing

        margins = self.contentsMargins()
        area = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )

        col_w = (max(area.width(), 0) - hs * (n - 1)) / n
        if col_w < 0:
            col_w = 0.0
        unit = col_w + hs

        # 跳过非 widget item（row = -1）与隐藏控件
        rows: dict[int, list[tuple[QWidgetItem, int, int]]] = {}
        for entry in self._items:
            if entry.row < 0 or entry.widget_item.isEmpty():
                continue
            rows.setdefault(entry.row, []).append(
                (entry.widget_item, entry.col, entry.colspan)
            )

        if not rows:
            return margins.top() + margins.bottom()

        y = area.y()
        sorted_rows = sorted(rows)

        for idx, row in enumerate(sorted_rows):
            row_h = 0
            for item, col, colspan in rows[row]:
                w = max(0, int(colspan * unit - hs))
                hfw = item.heightForWidth(w)
                h = hfw if hfw > 0 else item.sizeHint().height()
                row_h = max(row_h, h)

            if not test_only:
                for item, col, colspan in rows[row]:
                    x = int(area.x() + col * unit)
                    w = max(0, int(colspan * unit - hs))
                    item.setGeometry(QRect(x, y, w, row_h))

            y += row_h
            if idx < len(sorted_rows) - 1:
                y += vs

        return (y - rect.y()) + margins.bottom()
