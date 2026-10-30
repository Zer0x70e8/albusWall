#
"""Dispatcher —— 已解析 (path, args, kwargs) 的执行器。"""

from __future__ import annotations

from typing import Any, Callable

from albuswall.command import CommandRegistry
from albuswall.command.model import Command, Param
from albuswall.command.types import _EMPTY
from albuswall.log import getLogger

from .exceptions import (
    ArgumentBindingError, CommandFailed, CommandNotExecutable,
    DispatchError, UnknownCommand,
)

logger = getLogger(__name__)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _coerce(p: Param, value: Any) -> Any:
    if p.choices is not None and value not in p.choices:
        raise ArgumentBindingError(
            f"arg {p.name!r}: {value!r} not in choices {p.choices!r}"
        )
    return value


class Dispatcher:
    """把 (path, args, kwargs) 分派到已注册的命令。

    不解析协议 dict —— 那是 CommandService 的职责。
    """

    def __init__(
            self,
            registry: CommandRegistry,
            *,
            subject_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._subject_provider = subject_provider or (lambda: _EMPTY)

    def dispatch(
            self,
            path: str,
            args: list[Any],
            kwargs: dict[str, Any],
            *,
            subject: frozenset[str] | None = None,
    ) -> Any:
        cmd = self._registry.resolve(path)
        if cmd is None:
            raise UnknownCommand(path)
        if subject is None:
            subject = self._subject_provider()
        verdict = self._registry.check_executable(cmd.path, subject)
        if not verdict.ok:
            # 判序：先外部、再命名空间。``check_executable`` 对
            # ``external=True`` 的节点（通常 ``func is None``）返回
            # "external"，若先判 ``func is None`` 会把它错标成 "namespace"。
            if verdict.reason == "external":
                raise CommandNotExecutable(cmd.path, "external")
            if cmd.func is None:
                raise CommandNotExecutable(cmd.path, "namespace")
            raise CommandNotExecutable(cmd.path, verdict.reason)
        if cmd.func is None:
            # 兜底：正常注册下不可达——非 external 且 ``func is None`` 的
            # 节点，``check_executable`` 已判为 "disabled" 并在上面抛出。
            raise CommandNotExecutable(cmd.path, "namespace")

        if cmd.pass_raw:
            call_args: tuple = (list(args), dict(kwargs))
            call_kwargs: dict = {}
        else:
            bound_args, bound_kwargs = self._bind(cmd, args, kwargs)
            call_args, call_kwargs = tuple(bound_args), bound_kwargs

        if cmd.pass_subject:
            call_kwargs["subject"] = subject

        logger.debug(
            "dispatch %r args=%r kwargs=%r (subject=%r)",
            cmd.path, call_args, call_kwargs, subject,
        )
        try:
            return cmd.func(*call_args, **call_kwargs)
        except DispatchError:
            raise
        except Exception as e:
            raise CommandFailed(cmd.path, e) from e

    # -- 参数绑定 --------------------------------------------------

    def _bind(
            self, cmd: Command, args: list[Any], kwargs: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        params = cmd.params
        if not params:
            return list(args), dict(kwargs)

        pos = [p for p in params if p.kind == "pos"]
        opts: dict[str, Param] = {}
        flags: dict[str, Param] = {}
        for p in params:
            if p.kind == "opt":
                opts[p.name] = p
                if p.short:
                    opts[p.short] = p
            elif p.kind == "flag":
                flags[p.name] = p
                if p.short:
                    flags[p.short] = p

        # 位置参数
        if len(args) > len(pos):
            raise ArgumentBindingError(
                f"{cmd.path!r}: too many positional args "
                f"(expected {len(pos)}, got {len(args)})"
            )
        bound_args: list[Any] = []
        for i, p in enumerate(pos):
            if i < len(args):
                bound_args.append(_coerce(p, args[i]))
            elif p.required:
                raise ArgumentBindingError(
                    f"{cmd.path!r}: missing required positional {p.name!r}"
                )
            else:
                bound_args.append(p.default)

        # 关键字参数
        bound_kwargs: dict[str, Any] = {}
        used: set[str] = set()
        for key, value in kwargs.items():
            p = opts.get(key) or flags.get(key)
            if p is None:
                raise ArgumentBindingError(
                    f"{cmd.path!r}: unknown option {key!r}"
                )
            if p.name in used:
                raise ArgumentBindingError(
                    f"{cmd.path!r}: duplicate option {p.name!r}"
                )
            used.add(p.name)
            bound_kwargs[p.name] = (
                _as_bool(value) if p.kind == "flag" else _coerce(p, value)
            )

        # 补缺省
        for p in params:
            if p.kind == "pos" or p.name in used:
                continue
            if p.kind == "flag":
                bound_kwargs[p.name] = (
                    p.default if p.default is not None else False
                )
            elif p.required:
                raise ArgumentBindingError(
                    f"{cmd.path!r}: missing required option {p.name!r}"
                )
            else:
                bound_kwargs[p.name] = p.default

        return bound_args, bound_kwargs
