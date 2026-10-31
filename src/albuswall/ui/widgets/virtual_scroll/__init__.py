#
"""Virtual scroll widgets"""

from .delegate import CellDelegate, CellOption, PixmapCellDelegate
from .basic import VirtualScrollWidget as VirtualScrollWidgetBasic
from .styled import StyledVirtualScrollWidget

VirtualScrollWidget = StyledVirtualScrollWidget

__all__ = [
    "StyledVirtualScrollWidget",
    "VirtualScrollWidget",
    "VirtualScrollWidgetBasic",
    "CellDelegate",
    "CellOption",
    "PixmapCellDelegate",
]