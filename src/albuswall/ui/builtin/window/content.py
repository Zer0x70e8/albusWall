#
""""""

from ..widgets.virtual_scroll import VirtualScrollWidget


class Content(VirtualScrollWidget):
    # TODO 与presenter的接口放这里
    def __init__(self, parent=None):
        super().__init__(parent)
