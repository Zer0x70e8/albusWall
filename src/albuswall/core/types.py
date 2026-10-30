#
""""""

from types import TracebackType
from typing import TYPE_CHECKING, Callable, Protocol, TypedDict, Union, Literal

if TYPE_CHECKING:
    from .application import Application


class LoopHook(Protocol):
    def __call__(self, app: "Application") -> None: ...


class ErrorHook(Protocol):
    def __call__(
            self,
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_traceback: TracebackType | None,
    ) -> bool:
        """Return True if the error has been handled.

        Handled errors suppress the default traceback printer,
        but all hooks are still invoked.
        """
        ...


SimpleHook = Callable[[], None]
Hooks = Union[SimpleHook, ErrorHook, LoopHook]
HookNames = Literal[
    "boot",
    "init",
    "loop",
    "exit",
    "on_error",
    "on_fatal",
]


class HooksMap(TypedDict):
    boot: list[SimpleHook]
    init: list[SimpleHook]
    loop: list[LoopHook]
    exit: list[SimpleHook]
    on_error: list[ErrorHook]
    on_fatal: list[ErrorHook]
