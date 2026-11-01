# 
"""Bridge ``sys.excepthook`` to registered ``on_error`` hooks."""

import sys
from types import TracebackType

from albuswall.utils.hooks import run_exception_hooks

from .registry import hooks_map

_original_excepthook = sys.excepthook
_installed = False


def _dispatch_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
) -> None:
    handled = run_exception_hooks(
        hooks_map["on_error"], exc_type, exc_value, exc_traceback,
    )
    if not handled:
        _original_excepthook(exc_type, exc_value, exc_traceback)


def install_excepthook() -> None:
    """Install the dispatcher. Idempotent."""
    global _installed
    if _installed:
        return
    sys.excepthook = _dispatch_exception
    _installed = True


def uninstall_excepthook() -> None:
    """Restore the original hook. Useful in tests."""
    global _installed
    if not _installed:
        return
    sys.excepthook = _original_excepthook
    _installed = False
