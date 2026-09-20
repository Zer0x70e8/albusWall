#
""""""

from typing import TYPE_CHECKING

# from .bootstrap import setup_log
from .common import TRACE
from .type import Logger

if TYPE_CHECKING:
    from albuswall.core import Container
    def setup_log(_: "Container"):...

__all__ = ["TRACE", "Logger", "setup_log"]

def __getattr__(name):
    if name == "setup_log":
        from .bootstrap import setup_log
        return setup_log
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
