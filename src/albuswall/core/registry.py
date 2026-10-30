#
"""Global hook registry."""

from .types import HooksMap, Hooks, HookNames

__all__ = ["hooks_map", "register"]

hooks_map: HooksMap = {
    "boot": [], "init": [], "loop": [], "exit": [],
    "on_error": [], "on_fatal": [],
}


def register(event: HookNames, hook: Hooks) -> None:
    """Append a hook. No dedup — call it once per hook."""
    hooks_map[event].append(hook)  # type: ignore[arg-type]
