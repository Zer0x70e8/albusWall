#
"""Hooks shipped with the framework."""

import logging
from types import TracebackType

from .registry import hooks_map

logger = logging.getLogger(__package__)


def _builtin_exception_logger(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
) -> bool:
    """Log unhandled exceptions. Returns ``False`` so traceback still prints."""
    logger.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
    return False


def register_builtin_hooks() -> None:
    """Register framework-provided hooks. Idempotent."""
    if _builtin_exception_logger not in hooks_map["on_error"]:
        hooks_map["on_error"].append(_builtin_exception_logger)
