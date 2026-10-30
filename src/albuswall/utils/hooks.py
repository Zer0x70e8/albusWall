#
"""Hook runner utilities."""

import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType

__all__ = ["run_hooks", "run_exception_hooks", "ExceptionHook"]

ExceptionHook = Callable[
    [type[BaseException], BaseException, TracebackType | None],
    bool,
]


def run_hooks(
        hooks: Sequence[Callable[..., object]],
        *,
        args: Sequence[object] = (),
        kwargs: Mapping[str, object] | None = None,
        lifo: bool = False,
) -> None:
    """Run hooks in order, discarding their return values."""
    if kwargs is None:
        kwargs = {}
    for hook in (reversed(hooks) if lifo else hooks):
        hook(*args, **kwargs)


def _report_hook_error(exc: BaseException) -> None:
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def run_exception_hooks(
        hooks: Sequence[ExceptionHook],
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
        *,
        lifo: bool = False,
        on_hook_error: Callable[[BaseException], None] | None = None,
) -> bool:
    """Run exception hooks until one returns True.

    Returns:
        True  — some hook handled the exception (short-circuit),
        False — no hook handled it.

    A hook that raises does not abort the loop: the exception is handed to
    ``on_hook_error`` (defaults to printing to stderr with a traceback) and
    iteration continues. ``KeyboardInterrupt`` and ``SystemExit`` raised by
    a hook still propagate — they are control signals, not hook failures.
    """
    report = on_hook_error or _report_hook_error
    for hook in (
            reversed(hooks)
            if lifo else hooks
    ):
        try:
            handled = hook(exc_type, exc_value, exc_traceback)
        except Exception as hook_exc:
            report(hook_exc)
            continue
        if handled:
            return True
    return False
