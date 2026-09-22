#
""""""

from typing import Protocol, Callable
from PySide6.QtCore import QRect


class SearchAnimator(Protocol):
    def start_expand(self, start: QRect, end: QRect,
                     on_finished: Callable[[], None]) -> None: ...

    def start_collapse(self, start: QRect, end: QRect,
                       on_finished: Callable[[], None]) -> None: ...

    def update_target(self, rect: QRect) -> None: ...

    def cancel(self) -> None: ...
