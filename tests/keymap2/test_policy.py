"""绑定策略：系统保留键、修饰键要求、冲突查询。"""
from __future__ import annotations

import pytest

from albuswall.ui.builtin.keymap.model import Binding, Registry
from albuswall.ui.builtin.keymap.policy import (
    conflicts,
    reserved_action,
    validate_binding,
)


def _registry(*entries: tuple[str, str]) -> Registry:
    store = Registry()
    for action_id, _key in entries:
        store.register(_action(action_id))
    for action_id, key in entries:
        store.add_operation(key, action_id)
    return store


def _action(action_id: str):
    from albuswall.ui.builtin.keymap.model import Action

    def handler(ctx):  # noqa: ARG001
        return True

    return Action(
        id=action_id, label=action_id, description="demo",
        handler=handler, signature=None, takes_context=True,
    )


def test_delete_is_reserved_for_trash():
    assert reserved_action("Delete") == "asset.trash"
    assert reserved_action("ctrl+shift+delete") == "asset.empty_trash"
    assert reserved_action("ctrl+q") is None

    validate_binding("Delete", "asset.trash")
    validate_binding("Ctrl+Shift+Delete", "asset.empty_trash")
    with pytest.raises(ValueError):
        validate_binding("Delete", "asset.copy")
    with pytest.raises(ValueError):
        validate_binding("Ctrl+Shift+Delete", "asset.trash")


def test_custom_album_move_requires_command_modifier():
    validate_binding("Ctrl+Alt+1", "asset.move_to_album")
    validate_binding("Ctrl+Shift+M", "asset.move_to_album")
    with pytest.raises(ValueError):
        validate_binding("Shift+M", "asset.move_to_album")
    with pytest.raises(ValueError):
        validate_binding("M", "asset.move_to_album")


def test_conflicts_excludes_the_same_action():
    store = _registry(("asset.trash", "ctrl+t"), ("asset.copy", "ctrl+t"))
    assert [a.id for a in conflicts(store, "ctrl+t")] == [
        "asset.trash", "asset.copy"]
    assert [a.id for a in conflicts(store, "ctrl+t", action_id="asset.copy")] == [
        "asset.trash"]
    assert conflicts(store, "ctrl+u") == ()


def test_conflicts_accepts_store_without_bindings():
    class _NoBindings:
        pass

    assert conflicts(_NoBindings(), "ctrl+t") == ()


def test_binding_equality_is_arg_sensitive():
    assert Binding("asset.rating", (("rating", 1),)) != Binding(
        "asset.rating", (("rating", 2),))


def test_load_rejects_bindings_that_violate_policy():
    store = Registry()
    for action_id in ("asset.copy", "asset.move_to_album"):
        store.register(_action(action_id))
    missing = store.apply_bindings({
        "delete": (Binding("asset.copy"),),
        "m": (Binding("asset.move_to_album"),),
        "ctrl+alt+1": (Binding("asset.move_to_album"),),
    })
    assert "delete:asset.copy" in missing
    assert "m:asset.move_to_album" in missing
    assert "ctrl+alt+1:asset.move_to_album" not in missing
