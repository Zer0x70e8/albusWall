#
"""Global hook registry."""

from .types import HooksMap, Hooks, HookNames

__all__ = ["hooks_map", "register", "unregister"]

hooks_map: HooksMap = {
    "boot": [], "init": [],
    "loop": [], "exit": [],
    "on_error": [], "on_fatal": [],
    "select_config": [], "select_plugins": [], "select_loop": [],
}


def register(event: HookNames, hook: Hooks) -> None:
    """Append a hook. No dedup — call it once per hook."""
    hooks_map[event].append(hook)  # type: ignore[arg-type]


def unregister(event: HookNames, hook: Hooks) -> bool:
    """Remove a hook. Returns True if removed.

    撤销是插件回滚的唯一手段：``PluginContext`` 记录自己追加的每个 hook，
    setup 失败时逐个撤销。同一个 hook 重复登记时只会撤销一个（与 ``register``
    不去重的语义对称）。
    """
    try:
        hooks_map[event].remove(hook)  # type: ignore[arg-type]
        return True
    except ValueError:
        return False
