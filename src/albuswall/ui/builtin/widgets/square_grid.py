#
"""视图层：通用正方形网格视图。

基于 QListView 的 IconMode，天然虚拟化——无论多少条数据，
只有视口内的 item 会被绘制。

本类只负责布局 / 滚动 / 几何计算；模型与委托由调用方注入。
圆角、内边距、缩略图等视觉细节一律由委托持有，本视图不做假设。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QListView, QSizePolicy


# noinspection pep8-naming
class SquareGridView(QListView):
    """水平滚动的正方形网格视图（通用）。

    参数:
        ratio: 视口宽度内可容纳的正方形个数（单行维度），默认 3.0
        rows:  行数，默认 2；>1 时按列优先填充

    用法:
        view = SquareGridView(ratio=3.0, rows=2)
        view.setModel(my_model)
        view.setItemDelegate(my_delegate)
        view.clicked.connect(my_slot)

    说明:
        - 本视图不自带 model / delegate，需由调用方注入；
        - 不发射任何数据相关信号，直接用 QListView 自带的
          ``clicked`` / ``doubleClicked`` / ``activated`` 等即可；
        - 需要自定义外观时，直接配置你注入的 delegate（推荐保留
          一份引用），视图不再提供圆角 / 内边距等转发接口。
    """

    def __init__(self, ratio: float = 3.0, rows: int = 2, parent=None):
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
        self._apply_grid()

    # ---------- 属性 ----------
    def ratio(self) -> float:
        return self._ratio

    def setRatio(self, ratio: float) -> None:
        ratio = max(0.5, float(ratio))
        if ratio == self._ratio:
            return
        self._ratio = ratio
        self._apply_grid()
        self.updateGeometry()

    def rows(self) -> int:
        return self._rows

    def setRows(self, rows: int) -> None:
        rows = max(1, int(rows))
        if rows == self._rows:
            return
        self._rows = rows
        self.updateGeometry()

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

    # ---------- 布局 ----------
    def _cell_size(self) -> int:
        vw = self.viewport().width()
        if vw <= 0:
            return 1
        # viewport = ratio*cell + (ratio-1)*spacing
        raw = (vw - (self._ratio - 1) * self._spacing) / self._ratio
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
        cell = max(
            1,
            int((vw - (self._ratio - 1) * self._spacing) / self._ratio),
        )
        total = self._rows * cell + (self._rows - 1) * self._spacing
        return total + frame

    def sizeHint(self) -> QSize:
        w = 300
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()
