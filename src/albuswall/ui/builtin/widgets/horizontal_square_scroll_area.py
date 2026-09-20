#
""""""

import shiboken6
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtWidgets import (
    QScrollArea, QWidget,
    QSizePolicy, QPushButton, QGridLayout
)


# noinspection PyPep8Naming
class Item(QPushButton):
    selected = Signal(str)

    def __init__(self, /, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pixmap = None
        self._album_id: str = ""

        self.setObjectName("AlbumItem")
        self.clicked.connect(self._on_clicked)

    def set_album_id(self, album_id: str):
        """绑定相簿的唯一标识（UUID）"""
        self._album_id = album_id

    def _on_clicked(self):
        """内部槽，转发带 UUID 的信号"""
        if self._album_id:
            self.selected.emit(self._album_id)

    def setPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def pixmap(self) -> QPixmap:
        return self._pixmap

    def paintEvent(self, event):
        # 先让父类把按钮的默认外观（背景、文字等）画出来
        super().paintEvent(event)
        if (self._pixmap is not None) and (not self._pixmap.isNull()):
            painter = QPainter(self)
            # 例如按比例缩放到按钮内部并居中
            target = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            x = (self.width() - target.width()) // 2
            y = (self.height() - target.height()) // 2
            painter.drawPixmap(x, y, target)
            painter.end()


# noinspection PyPep8Naming
class HorizontalSquareScrollArea(QScrollArea):
    """
    水平滚动的正方形项目区域。

    参数:
        ratio: 视口宽度内可容纳的正方形个数（单行维度），默认 1.5
        rows:  行数，默认 1（即单行水平滚动），设为 2 即为双排
    """

    def __init__(self, ratio=1.5, rows=1, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ratio = max(0.5, ratio)
        self._rows = max(1, rows)
        self._spacing = 5
        self._items = []
        self._updating = False
        self._pending = False  # 是否有待处理的布局请求

        self._container = QWidget()
        # 改用网格布局，先行后列填放
        self._layout = QGridLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self._spacing)

        self.setWidget(self._container)
        self.setWidgetResizable(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # 高度依赖宽度和行数，不设置固定样式
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def ratio(self):
        return self._ratio

    def setRatio(self, ratio):
        self._ratio = max(0.5, ratio)
        self._updateLayout()
        self.updateGeometry()

    def rows(self):
        return self._rows

    def setRows(self, rows):
        """设置行数，触发重新布局"""
        rows = max(1, rows)
        if rows != self._rows:
            self._rows = rows
            self._updateLayout()
            self.updateGeometry()

    def addWidget(self, widget):
        self._items.append(widget)
        self._updateLayout()

    def removeWidget(self, widget):
        """移除一个控件"""
        if widget in self._items:
            self._items.remove(widget)
            self._layout.removeWidget(widget)
            widget.setParent(None)  # 可选：若需彻底删除则调用 widgets.deleteLater()
            self._updateLayout()

    def clear(self):
        """清空所有项目"""
        for widget in self._items:
            self._layout.removeWidget(widget)
        self._items.clear()
        self._updateLayout()

    def setSpacing(self, spacing):
        self._spacing = spacing
        self._layout.setSpacing(spacing)
        self._updateLayout()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        if width <= 0:
            return 0
        frame = self.frameWidth() * 2
        viewport_width = max(0, width - frame)
        square_size = viewport_width / self._ratio
        # 总高度 = 行数 × 正方形边长 + (行数-1) × 间距
        total_height = self._rows * square_size + (self._rows - 1) * self._spacing
        return max(0, int(total_height)) + frame

    def sizeHint(self):
        w = 300
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self):
        return self.sizeHint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._updateLayout()

    def _updateLayout(self):
        """根据当前视口宽度、ratio、行数重新计算正方形尺寸并排列项目"""
        # 必须加锁，要不然会导致线程崩溃
        if self._updating:
            self._pending = True  # 标记有待处理的请求
            return
        self._updating = True
        self._pending = False
        try:
            # --- 防御：如果容器或布局已失效，就重新初始化 ---
            if (self._container is None or
                    not shiboken6.isValid(self._container) or
                    not shiboken6.isValid(self._layout)):

                # 重新创建容器和布局
                if self._container is not None:
                    # 断开旧容器与滚动区域的关系（如果还有效）
                    if shiboken6.isValid(self._container):
                        self.takeWidget()  # 安全取出，不删除容器
                self._container = QWidget()
                self._layout = QGridLayout(self._container)
                self._layout.setContentsMargins(0, 0, 0, 0)
                self._layout.setSpacing(self._spacing)
                self.setWidget(self._container)

            #
            viewport = self.viewport()
            if not viewport:
                return
            w = viewport.width()
            if w <= 0:
                return

            # 1. 彻底清空布局项（保留控件对象）
            while self._layout.count():
                self._layout.takeAt(0)
                # item = self._layout.takeAt(0)
                # # 不需要 delete，因为 widgets 仍然被 self._items 持有

            square_size = w / self._ratio
            item_size = QSize(int(square_size), int(square_size))
            n = len(self._items)
            cols = max(1, (n + self._rows - 1) // self._rows)

            # 2. 重新按照先行后列排列
            for idx, widget in enumerate(self._items):
                row = idx // cols
                col = idx % cols
                self._layout.addWidget(widget, row, col)
                widget.setFixedSize(item_size)

            # 3. 更新容器尺寸
            total_width = cols * square_size + (cols - 1) * self._spacing
            total_height = self._rows * square_size + (self._rows - 1) * self._spacing
            self._container.setFixedSize(int(total_width), int(total_height))
            self.updateGeometry()
        finally:
            self._updating = False
