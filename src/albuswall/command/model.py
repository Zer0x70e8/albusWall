#
"""Data model"""

import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .types import Handler, _EMPTY


@dataclass(frozen=True, slots=True)
class Param:
    """A single command-line parameter."""

    name: str
    kind: Literal["pos", "opt", "flag"] = "pos"
    type: Callable[[str], Any] = str
    default: Any = None
    required: bool = False
    choices: tuple[Any, ...] | None = None
    help: str = ""
    short: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("pos", "opt", "flag"):
            raise ValueError(
                f"Param {self.name!r}: kind must be 'pos', 'opt', or 'flag'"
            )
        if self.required and self.default is not None:
            raise ValueError(
                f"Param {self.name!r}: required params must not carry a default"
            )
        if self.kind == "flag" and self.type is not bool:
            raise ValueError(
                f"Param {self.name!r}: flag params must use type=bool"
            )
        if self.kind == "pos" and not self.required and self.default is None:
            warnings.warn(
                f"Param {self.name!r}: positional param with no default and "
                "required=False; consider required=True or kind='opt'",
                stacklevel=2,
            )


@dataclass
class Command:
    """A registered command or namespace node.

    ``func is None`` means "pure namespace". ``children`` stores full paths
    of descendants so lookups never walk the tree eagerly.
    """

    path: str
    func: Handler | None = None
    help: str = ""
    aliases: tuple[str, ...] = ()
    groups: frozenset[str] = _EMPTY
    acl: frozenset[str] = _EMPTY
    enabled: bool = True
    hidden: bool = False
    params: tuple[Param, ...] = ()
    pass_raw: bool = False
    pass_subject: bool = False
    external: bool = False  # 命令服务不执行，由外部层处理
    children: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ExecutionVerdict:
    """Result of :meth:`CommandRegistry.check_executable`."""

    ok: bool
    reason: Literal[
        "ok",
        "disabled",
        "acl",
        "authorizer",
        "external",
    ] = "ok"
