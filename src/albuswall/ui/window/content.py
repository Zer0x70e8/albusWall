#
"""内容层：窗口最底层的虚拟滚动网格。

内容层就是一个 :class:`~albuswall.ui.widgets.view.tile_view.TileView`（``QListView``
的等大方格封装），这里只补上「它在窗口里的身份」：

* ``objectName`` = ``Content`` —— 主题样式表（``#Content``）按名字命中它；
* 不带 model / delegate：数据由
  :class:`~albuswall.ui.presenter.content_presenter.ContentPresenter` 用
  :meth:`~albuswall.ui.widgets.view.tile_view.TileView.setModel` 投进来，缩略图
  异步回填到模型上（``AssetGridModel.set_pixmap``）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..widgets import TileView

__all__ = ["Content"]


class Content(TileView):
    """内容层容器（铺满窗口，压在所有图层最底）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # 主题用 ``#Content`` 命中这个视图（含 ``background: transparent``）。
        self.setObjectName("Content")
