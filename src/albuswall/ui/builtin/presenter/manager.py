#
""""""

from __future__ import annotations
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from albuswall.core import Container


@runtime_checkable
class Presenter(Protocol):
    def setup(self, container: "Container") -> None: ...

    def teardown(self) -> None: ...


class PresenterManager:
    """按注册顺序 setup、逆序 teardown，保证父→子初始化、子→父销毁。"""

    def __init__(self) -> None:
        self._presenters: list = []

    def add(self, presenter):
        self._presenters.append(presenter)
        return presenter  # 方便链式调用

    def setup(self, container: "Container") -> None:
        for p in self._presenters:
            setup = getattr(p, "setup", None)
            if callable(setup):
                # noinspection calling-non-callable
                setup(container)

    def teardown(self) -> None:
        for p in reversed(self._presenters):
            teardown = getattr(p, "teardown", None)
            if callable(teardown):
                # noinspection calling-non-callable
                teardown()
        self._presenters.clear()

    def __iter__(self):
        return iter(self._presenters)

    def __len__(self) -> int:
        return len(self._presenters)

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__}: (",
            *[
                f"\t{type(i).__name__}: " +
                "\n\t".join(str(i).splitlines())
                for i in self
            ],
            ")",
        ))

    def format(self, index=0, indent="\t"):
        return "\n".join((
            f"{indent * index}{type(self).__name__}: (",
            *[f"{indent * (index + 1)}{type(i).__name__}: " +
              f"\n{(indent * (index + 1))}".join(
                  str(i).splitlines()
              ) for i in self],
            f"{indent * index})",
        ))
