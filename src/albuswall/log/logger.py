#
""""""

from logging import getLogger as _getLogger

from .common import TRACE
from .type import Logger


# noinspection pep8-naming, bad-assignment
def getLogger(name: str | None = None) -> Logger:
    logger: Logger = _getLogger(name)
    logger.trace = lambda msg, *args: logger.log(TRACE, msg, *args)
    return logger
