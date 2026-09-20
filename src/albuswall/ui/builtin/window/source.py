#
""""""

from PySide6.QtWidgets import QVBoxLayout

from ..widgets import BlurLabel, ColumnListView

class IngestSource(BlurLabel):
    main_layout: QVBoxLayout

    view: ColumnListView
    def __init__(self, *args):
        super().__init__(*args)


