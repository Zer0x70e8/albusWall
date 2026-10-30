#
""""""

from typing import TYPE_CHECKING

from .common import TRACE, to_levelno
from .type import Logger
from .logger import getLogger

if TYPE_CHECKING:
    from albuswall.core import Container


    def setup_log(_: "Container") -> None: ...


    # noinspection unused-parameter
    def setup_cli_logging(name: str = "", **kwargs) -> Logger: ...

# noinspection SpellCheckingInspection
__all__ = ["TRACE", "to_levelno", "Logger", "getLogger",
           "setup_log", "setup_cli_logging"]


def __getattr__(name):
    if name == "setup_log":
        from .bootstrap import setup_log
        return setup_log
    if name == "setup_cli_logging":
        from .cli import setup_cli_logging
        return setup_cli_logging
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
