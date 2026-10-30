#
"""Command registry: namespace tree, permissions, and parameter schema.

Holds the ``CommandRegistry`` used by the CLI, GUI menus, and plugin system
for command introspection.

Field rationale:

- ``Command`` is not frozen because internal nodes start empty and later
  receive ``func`` when a command registers on the same path; ``children``
  also grows as subcommands are added.
- ``Param`` is frozen+slots so schemas are hashable and reusable.
- ACL lives on ``Command.acl``; rules are expressed by the pluggable
  ``Authorizer`` callback.

Lifecycle notes:

- :meth:`CommandRegistry.freeze` prevents further registration, but
  :meth:`CommandRegistry.set_authorizer` remains callable — it is runtime
  policy, not registration.
- :meth:`CommandRegistry.clear` (test helper) resets nodes, indices, **and
  the authorizer**; a stale callable is the most common source of cross-test
  pollution.

Permission model:

- ``subject=frozenset()`` means **anonymous**, never "skip permissions".
- :meth:`CommandRegistry.visible` is hierarchical (menu fold/unfold).
- :meth:`CommandRegistry.names` is flat: each node's own ``groups`` gate is
  checked without ancestor chaining.

TODO(ui): keyboard shortcut binding (defaults, user overrides, conflict
detection, scope, platform normalization) is owned by the UI layer and
intentionally has no surface on the registry.
"""

from __future__ import annotations

from typing import Callable

from .exceptions import InvalidCommandName, DuplicateCommand, RegistryFrozen
from .model import Command, Param, ExecutionVerdict
from .types import Authorizer, _EMPTY, Handler

__all__ = [
    "CommandRegistry", "resolve",
    "get_default_registry", "list_commands", "command",
    "_default",
]


class CommandRegistry:
    """Multi-instance command registry with namespaces, ACL and authorizer."""

    def __init__(self) -> None:
        self._nodes: dict[str, Command] = {}
        self._index: dict[str, str] = {}
        self._group_index: dict[str, set[str]] = {}
        self._authorizer: Authorizer | None = None
        self._frozen: bool = False

    # -- lifecycle -----------------------------------------------------

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """Prevent further registrations."""
        self._frozen = True

    def clear(self) -> None:
        """Reset nodes, indices, authorizer, and unfreeze.

        Test-only helper. The authorizer is reset because a stale callable
        is the most common source of cross-test pollution.
        """
        self._nodes.clear()
        self._index.clear()
        self._group_index.clear()
        self._authorizer = None
        self._frozen = False

    def set_authorizer(self, authorizer: Authorizer | None) -> None:
        """Install (or clear) the runtime policy callback.

        Unlike :meth:`register`, this is allowed after :meth:`freeze`
        because authorization is a runtime concern, not registration.
        Note that :meth:`clear` resets the authorizer to ``None``.
        """
        self._authorizer = authorizer

    # -- registration --------------------------------------------------

    def register(self, cmd: Command) -> None:
        """Register ``cmd`` and any missing parent namespaces."""
        if self._frozen:
            raise RegistryFrozen(cmd.path)

        path = cmd.path
        parts = path.split(".")
        if not path or any(not p for p in parts):
            raise InvalidCommandName(path)

        existing = self._nodes.get(path)
        if existing is not None:
            # Fill-in is only allowed when the existing node is a bare
            # namespace and the incoming command carries a func.
            if cmd.func is None or existing.func is not None:
                raise DuplicateCommand(path)
            seen: set[str] = {path}
            for alias in cmd.aliases:
                if alias in self._index or alias in seen:
                    raise DuplicateCommand(alias)
                seen.add(alias)
            self._apply(existing, cmd)
            return

        parent_paths = [".".join(parts[:i]) for i in range(1, len(parts))]

        seen = {path}
        seen.update(parent_paths)
        for alias in cmd.aliases:
            if alias in self._index or alias in seen:
                raise DuplicateCommand(alias)
            seen.add(alias)
        if path in self._index:
            raise DuplicateCommand(path)

        # Auto-create missing parents.
        for i, parent in enumerate(parent_paths, start=1):
            if parent in self._nodes:
                continue
            self._nodes[parent] = Command(path=parent)
            if i > 1:
                self._nodes[parent_paths[i - 2]].children.add(parent)
            self._index[parent] = parent

        self._nodes[path] = cmd
        self._index[path] = path
        for alias in cmd.aliases:
            self._index[alias] = path
        for group in cmd.groups:
            self._group_index.setdefault(group, set()).add(path)
        if parent_paths:
            self._nodes[parent_paths[-1]].children.add(path)

    def _apply(self, existing: Command, cmd: Command) -> None:
        """Merge ``cmd`` fields onto ``existing`` (fill-in case).

        Cleans up stale group and alias index entries so that aliases/groups
        removed by the new command no longer resolve.
        """
        # Detach groups that no longer apply.
        for group in existing.groups - cmd.groups:
            bucket = self._group_index.get(group)
            if bucket is not None:
                bucket.discard(existing.path)
                if not bucket:
                    del self._group_index[group]

        # Detach stale aliases before overwriting.
        old_aliases = set(existing.aliases)
        new_aliases = set(cmd.aliases)
        for alias in old_aliases - new_aliases:
            self._index.pop(alias, None)

        existing.func = cmd.func
        existing.help = cmd.help
        existing.aliases = cmd.aliases
        existing.groups = cmd.groups
        existing.acl = cmd.acl
        existing.enabled = cmd.enabled
        existing.hidden = cmd.hidden
        existing.params = cmd.params
        existing.pass_raw = cmd.pass_raw
        existing.external = cmd.external
        existing.pass_subject = cmd.pass_subject

        for alias in cmd.aliases:
            self._index[alias] = existing.path
        for group in cmd.groups:
            self._group_index.setdefault(group, set()).add(existing.path)

    def command(
            self,
            path: str,
            *,
            help_: str = "",
            aliases: tuple[str, ...] = (),
            groups: frozenset[str] = _EMPTY,
            acl: frozenset[str] = _EMPTY,
            enabled: bool = True,
            hidden: bool = False,
            params: tuple[Param, ...] = (),
            pass_raw: bool = False,
            pass_subject: bool = False,
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`."""

        if pass_subject and any(p.name == "subject" for p in params):
            raise ValueError(f"{path!r}: param 'subject' conflicts with pass_subject")

        def decorator(func: Handler) -> Handler:
            self.register(
                Command(
                    path=path,
                    func=func,
                    help=help_,
                    aliases=aliases,
                    groups=groups,
                    acl=acl,
                    enabled=enabled,
                    hidden=hidden,
                    params=params,
                    pass_raw=pass_raw,
                    pass_subject=pass_subject,
                )
            )
            return func

        return decorator

    def namespace(
            self,
            path: str,
            *,
            help_: str = "",
            groups: frozenset[str] = _EMPTY,
            hidden: bool = False,
    ) -> None:
        """Explicitly register a bare namespace node."""
        self.register(
            Command(path=path, help=help_, groups=groups, hidden=hidden)
        )

    # -- queries -------------------------------------------------------

    def get(self, path: str) -> Command | None:
        """Return the node registered at ``path`` (exact match)."""
        return self._nodes.get(path)

    def resolve(self, name: str) -> Command | None:
        """Look up by primary path or alias."""
        path = self._index.get(name)
        if path is None:
            return None
        return self._nodes.get(path)

    @staticmethod
    def _passes_groups(cmd: Command, subject: frozenset[str]) -> bool:
        """Groups gate.

        ``cmd.groups`` non-empty and disjoint from ``subject`` → False.
        Empty ``groups`` = public (everyone passes); empty ``subject`` =
        anonymous (belongs to no group, so only public commands pass).
        """
        return not cmd.groups or bool(cmd.groups & subject)

    def names(
            self,
            *,
            include_hidden: bool = False,
            subject: frozenset[str] = _EMPTY,
    ) -> list[str]:
        """Sorted list of command paths (bare namespaces excluded).

        ``subject=frozenset()`` means **anonymous** — it does not bypass
        permissions. To obtain the full command inventory (admin or
        introspection), pass explicit permission groups such as
        ``{"admin"}``.

        注意：``names()`` 是扁平列表，**不做祖先链传导**——每个节点只检查
        自己的 ``groups`` 门禁。父命名空间标了 ``admin`` 但子命令公开时，
        匿名用户仍会列出该子命令（有意设计）。需要层级/菜单折叠语义请用
        :meth:`visible`。
        """
        result: list[str] = []
        for path, cmd in self._nodes.items():
            if cmd.func is None and not cmd.external:
                continue
            if not include_hidden and cmd.hidden:
                continue
            if not self._passes_groups(cmd, subject):
                continue
            result.append(path)
        return sorted(result)

    def by_group(
            self,
            *,
            include_hidden: bool = False,
            subject: frozenset[str] = _EMPTY,
    ) -> dict[str, list[str]]:
        """Group name → sorted command paths that pass ``subject``'s gate.

        ``subject=frozenset()`` is anonymous; no ancestor chaining. A group
        whose members are all blocked appears as an empty list.
        """
        result: dict[str, list[str]] = {}
        for group, paths in self._group_index.items():
            bucket: list[str] = []
            for p in paths:
                cmd = self._nodes[p]
                if cmd.func is None and not cmd.external:
                    continue
                if not include_hidden and cmd.hidden:
                    continue
                if not self._passes_groups(cmd, subject):
                    continue
                bucket.append(p)
            result[group] = sorted(bucket)
        return result

    def describe(
            self,
            *,
            include_hidden: bool = False,
            subject: frozenset[str] = _EMPTY,
    ) -> dict[str, str]:
        """Path → first line of help_ (no docstring fallback).

        Hidden commands are excluded unless ``include_hidden=True``, matching
        :meth:`names`. ``subject=frozenset()`` is anonymous; no ancestor
        chaining.
        """
        result: dict[str, str] = {}
        for path, cmd in self._nodes.items():
            if cmd.func is None and not cmd.external:
                continue
            if not include_hidden and cmd.hidden:
                continue
            if not self._passes_groups(cmd, subject):
                continue
            result[path] = cmd.help.split("\n", 1)[0] if cmd.help else ""
        return result

    def visible(
            self,
            path: str,
            subject: frozenset[str] = _EMPTY,
    ) -> bool:
        """Whether ``path`` shows up in listings, completions and menus.

        Only ``hidden`` and ``groups`` are considered. ``acl``, ``enabled``
        and the authorizer are **not** consulted here - a visible command
        can still be non-executable, and a hidden one can still execute.
        """
        cmd = self._nodes.get(path)
        if cmd is None:
            return False
        if cmd.hidden:
            return False
        if cmd.groups and not (cmd.groups & subject):
            return False
        if not cmd.children:
            return cmd.func is not None or cmd.external
        if cmd.func is not None or cmd.external:
            return True
        return any(self.visible(child, subject) for child in cmd.children)

    def check_executable(
            self,
            path: str,
            subject: frozenset[str] = _EMPTY,
    ) -> ExecutionVerdict:
        """Whether ``subject`` may actually execute ``path``.

        Order of checks:

        1. missing or namespace node -> ``disabled``
        2. ``enabled=False``         -> ``disabled``
        3. ACL / groups gate         -> ``acl``
        4. authorizer denial         -> ``authorizer``

        Important: when ``acl`` is non-empty it **fully replaces** ``groups``
        for execution. ``groups`` only affects :meth:`visible`. Use this to
        model read-only modes: ``acl={"readonly"}, groups={"admin"}`` makes
        admin users *see* the command but not run it.
        """
        # 这条会打破"GUI 也可能直接传名字"的兼容性——建议只在文档加粗，不加 raise。 明确"不推荐"。
        # if path in self._index and self._index[path] != path:
        #     raise ValueError(f"check_executable got alias {path!r}; "
        #                      f"use {self._index[path]!r}")
        cmd = self._nodes.get(path)
        if cmd is None:
            return ExecutionVerdict(False, "disabled")
        if cmd.external:
            return ExecutionVerdict(False, "external")
        if cmd.func is None:
            return ExecutionVerdict(False, "disabled")
        if not cmd.enabled:
            return ExecutionVerdict(False, "disabled")
        need = cmd.acl if cmd.acl else cmd.groups
        if need and not (need & subject):
            return ExecutionVerdict(False, "acl")
        if self._authorizer is not None and not self._authorizer(path, subject):
            return ExecutionVerdict(False, "authorizer")
        return ExecutionVerdict(True, "ok")

    def check_executable_by_name(
            self,
            path: str,
            subject: frozenset[str] = _EMPTY,
    ) -> ExecutionVerdict:
        if path in self._index and self._index[path] != path:
            raise ValueError(f"check_executable got alias {path!r}; "
                             f"use {self._index[path]!r}")
        return self.check_executable(path, subject)

    def executable(
            self,
            path: str,
            subject: frozenset[str] = _EMPTY,
    ) -> bool:
        """Thin wrapper around :meth:`check_executable`."""
        return self.check_executable(path, subject).ok


# Default instance + module-level helpers

_default = CommandRegistry()


def get_default_registry() -> CommandRegistry:
    """Return the process-wide default registry."""
    return _default


def command(
        path: str,
        *,
        help_: str = "",
        aliases: tuple[str, ...] = (),
        groups: frozenset[str] = _EMPTY,
        acl: frozenset[str] = _EMPTY,
        enabled: bool = True,
        hidden: bool = False,
        params: tuple[Param, ...] = (),
        pass_raw: bool = False,
        pass_subject: bool = False,
) -> Callable[[Handler], Handler]:
    """Shorthand for ``_default.command(...)``."""
    return _default.command(
        path,
        help_=help_,
        aliases=aliases,
        groups=groups,
        acl=acl,
        enabled=enabled,
        hidden=hidden,
        params=params,
        pass_raw=pass_raw,
        pass_subject=pass_subject,
    )


def list_commands(
        *,
        include_hidden: bool = False,
        subject: frozenset[str] = _EMPTY,
) -> list[str]:
    """Shorthand for ``_default.names(...)``."""
    return _default.names(include_hidden=include_hidden, subject=subject)


def resolve(name: str) -> Command | None:
    """Shorthand for ``_default.resolve(name)``."""
    return _default.resolve(name)
