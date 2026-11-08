#
"""视图层：通用正方形网格视图。

基于 QListView 的 IconMode，天然虚拟化——无论多少条数据，
只有视口内的 item 会被绘制。

本类只负责布局 / 滚动 / 几何计算；模型与委托由调用方注入。
圆角、内边距、缩略图等视觉细节一律由委托持有，本视图不做假设。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Property, QSize, Qt
from PySide6.QtWidgets import QFrame, QListView, QSizePolicy


# noinspection pep8-naming
class SquareGridView(QListView):
    """水平滚动的正方形网格视图（通用）。

    参数:
        ratio: 视口宽度内可容纳的正方形个数（可为小数），默认 2.5
               → 横向露出 2 整列 + 第 3 列一半
        rows:  每列竖向堆放的个数，默认 2

    用法:
        view = SquareGridView(ratio=2.5, rows=2)
        view.setModel(my_model)
        view.setItemDelegate(my_delegate)
        view.clicked.connect(my_slot)

    运行时修改:
        view.setRatio(3.5)
        view.setRows(3)
        # 或 QSS（Property 别名，避免与 QListView 现有属性冲突）:
        # SquareGridView { qproperty-gridRatio: 3.5; qproperty-gridRows: 3; }

    说明:
        - 本视图不自带 model / delegate，需由调用方注入；
        - 不发射任何数据相关信号，直接用 QListView 自带的
          ``clicked`` / ``doubleClicked`` / ``activated`` 等即可；
        - 布局参数只影响 QListView 的 gridSize 与高度，委托拿到的
          ``option.rect`` 会随之变化，但委托代码无需任何改动。
    """

    def __init__(self, ratio: float = 2.5, rows: int = 2, parent=None):
        super().__init__(parent)
        self._ratio = max(0.5, float(ratio))
        self._rows = max(1, int(rows))
        self._spacing = 5

        # --- view 行为 ---
        self.setViewMode(QListView.ViewMode.IconMode)
        # 列优先：从上往下铺满 rows 个，再换下一列
        self.setFlow(QListView.Flow.TopToBottom)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Fixed)
        self.setUniformItemSizes(True)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)

        # 让父布局使用 heightForWidth
        policy = QSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

        super().setSpacing(self._spacing)

    # ---------- QSS 属性别名（避免与 QListView 已有名字冲突） ----------
    def getGridRatio(self) -> float:
        return self._ratio

    def getGridRows(self) -> int:
        return self._rows

    gridRatio = Property(float, getGridRatio, lambda self, v: self.setRatio(v))
    gridRows = Property(int, getGridRows, lambda self, v: self.setRows(v))

    # ---------- 属性 ----------
    def ratio(self) -> float:
        return self._ratio

    def setRatio(self, ratio: float) -> None:
        ratio = max(0.5, float(ratio))
        if abs(ratio - self._ratio) < 1e-9:
            return
        self._ratio = ratio
        self._apply_grid()
        self.updateGeometry()
        self.scheduleDelayedItemsLayout()

    def rows(self) -> int:
        return self._rows

    def setRows(self, rows: int) -> None:
        rows = max(1, int(rows))
        if rows == self._rows:
            return
        self._rows = rows
        self.updateGeometry()
        self.scheduleDelayedItemsLayout()

    def spacing(self) -> int:
        return self._spacing

    def setSpacing(self, spacing: int) -> None:
        spacing = int(spacing)
        if spacing == self._spacing:
            return
        self._spacing = spacing
        super().setSpacing(spacing)
        self._apply_grid()
        self.updateGeometry()
        self.scheduleDelayedItemsLayout()

    # ---------- 布局 ----------
    def _visible_gaps(self) -> int:
        """视口内可见的“列间间距”个数。

        ratio=2.5 → 可见 2 整列 + 半列 → 间距 2 个
        ratio=3.0 → 3 整列 → 间距 2 个
        ratio=1.5 → 1 整列 + 半列 → 间距 1 个
        """
        return max(0, int(math.ceil(self._ratio - 1e-9)) - 1)

    def _hsb_height(self) -> int:
        """水平滚动条高度（AsNeeded 时预留，避免吃掉 rows 的竖向空间）。"""
        sb = self.horizontalScrollBar()
        if sb is not None and sb.sizeHint().height() > 0:
            return sb.sizeHint().height()
        return 0

    def _cell_size(self, viewport_width: int | None = None) -> int:
        vw = self.viewport().width() if viewport_width is None else viewport_width
        if vw <= 0:
            return 1
        gaps = self._visible_gaps()
        # vw = ratio*cell + gaps*spacing
        raw = (vw - gaps * self._spacing) / self._ratio
        return max(1, int(raw))

    def _apply_grid(self) -> None:
        cell = self._cell_size()
        new_grid = QSize(cell, cell)
        if self.gridSize() != new_grid:
            self.setGridSize(new_grid)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_grid()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if width <= 0:
            return 0
        frame = self.frameWidth() * 2
        vw = width - frame
        if vw <= 0:
            return frame
        gaps = self._visible_gaps()
        cell = max(
            1,
            int((vw - gaps * self._spacing) / self._ratio),
        )
        total = self._rows * cell + (self._rows - 1) * self._spacing
        # ★ 预留水平滚动条高度，保证 viewport 高度仍能放下 rows 个
        return total + frame + self._hsb_height()

    def sizeHint(self) -> QSize:
        w = 300
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_grid()

    # ---------- 数据接口 ----------
    def bind_model(self, model, delegate=None) -> "SquareGridView":
        """直接接收一份模型（和可选委托）并立即生效。

        :param model: 任意 ``QAbstractItemModel``；本项目里通常来自
            :mod:`albuswall.ui.model`（``AlbumModel`` / ``AssetGridModel``）。
        :param delegate: 委托；``None`` 时保留当前委托（本视图不自带默认值）。
        :return: ``self``，便于链式调用。
        """
        self.setModel(model)
        if delegate is not None:
            self.setItemDelegate(delegate)
        return self
