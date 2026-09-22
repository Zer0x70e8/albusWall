#
"""视图层：横向滚动的正方形网格。

基于 QListView 的 IconMode，天然虚拟化——无论多少条数据，
只有视口内的 item 会被绘制。
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QListView, QSizePolicy

from ..model.album_model import AlbumModel, AlbumRole
from ..delegate.album_delegate import AlbumDelegate


# noinspection pep8-naming
class SquareGridView(QListView):
    """水平滚动的正方形网格。

    参数:
        ratio: 视口宽度内可容纳的正方形个数（单行维度），默认 3.0
        rows:  行数，默认 2；>1 时按列优先填充
    """

    #: 点击某个 item 时发射，携带其 uuid
    albumSelected = Signal(str)

    def __init__(self, ratio: float = 3.0, rows: int = 2, parent=None):
        super().__init__(parent)
        self._ratio = max(0.5, float(ratio))
        self._rows = max(1, int(rows))
        self._spacing = 5

        # --- model / delegate ---
        self._model = AlbumModel(self)
        self.setModel(self._model)

        self._delegate = AlbumDelegate(self)
        self.setItemDelegate(self._delegate)

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

        self.clicked.connect(self._on_clicked)

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

    # ---------- 外观 ----------
    def setBorderRadius(self, radius: int) -> None:
        self._delegate.setBorderRadius(radius)
        self.viewport().update()

    def setItemPadding(self, padding: int) -> None:
        self._delegate.setPadding(padding)
        self.viewport().update()

    # ---------- 数据便捷接口 ----------
    def add_album(self, uuid: str, pixmap: QPixmap | None = None) -> int:
        """追加一张专辑，返回其 row。"""
        return self._model.append(uuid, pixmap)

    def set_album_pixmap(self, row: int, pixmap: QPixmap) -> None:
        """异步加载完成后回填缩略图。"""
        self._model.setPixmap(row, pixmap)

    def album_id_at(self, row: int) -> str:
        return self._model.uuid_at(row)

    def count(self) -> int:
        return self._model.rowCount()

    def clear(self) -> None:
        self._model.clear()

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

    # ---------- 交互 ----------
    def _on_clicked(self, index: QModelIndex) -> None:
        uuid = index.data(AlbumRole.Uuid)
        if uuid:
            self.albumSelected.emit(uuid)
