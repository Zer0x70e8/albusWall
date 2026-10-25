"""默认键位可以携带不同参数（评分 / 快捷槽位）。"""
from __future__ import annotations

import pytest

from albuswall.ui.builtin.keymap import model as model_module
from albuswall.ui.builtin.keymap.bindings import default_bindings, normalize_key
from albuswall.ui.builtin.keymap.model import Registry, shortcut


@pytest.fixture()
def store(monkeypatch):
    fake = Registry()
    monkeypatch.setattr(model_module, "registry", fake)
    return fake


def test_keys_mapping_carries_per_key_arguments(store):
    @shortcut(
        "demo.slot",
        description="move to slot",
        keys={f"Shift+{i}": {"slot": i} for i in range(1, 4)},
        arguments={"slot": 0},
    )
    def move(ctx, slot: int = 0) -> bool:  # noqa: ARG001
        return True

    action = store.get_action("demo.slot")
    assert action is not None
    assert action.keys == ("shift+1", "shift+2", "shift+3")
    assert action.arguments_for("shift+2") == (("slot", 2),)
    # 非默认键回落到动作级参数
    assert action.arguments_for("shift+9") == (("slot", 0),)

    table = default_bindings(store.iter_actions())
    assert table["shift+1"] == (store.bindings("shift+1")[0][1],)
    assert dict(table["shift+3"][0].args) == {"slot": 3}


def test_registry_default_bindings_use_key_arguments(store):
    @shortcut(
        "demo.rate",
        description="rate",
        keys={"0": {"rating": 0}, "5": {"rating": 5}},
        arguments={"rating": -1},
    )
    def rate(ctx, rating: int = -1) -> bool:  # noqa: ARG001
        return True

    defaults = store.default_bindings()
    assert dict(defaults["0"][0].args) == {"rating": 0}
    assert dict(defaults["5"][0].args) == {"rating": 5}


def test_key_arguments_are_validated(store):
    with pytest.raises(ValueError):
        @shortcut(
            "demo.bad",
            description="bad args",
            keys={"a": {"unknown": 1}},
        )
        def bad(ctx) -> bool:  # noqa: ARG001
            return True


def test_actions_for_unknown_key_returns_action_arguments(store):
    @shortcut("demo.plain", description="plain", keys="Ctrl+D")
    def plain(ctx) -> bool:  # noqa: ARG001
        return True

    action = store.get_action("demo.plain")
    assert action is not None
    assert action.arguments_for("ctrl+d") == ()
    assert action.arguments_for("ctrl+alt+d") == ()


@pytest.mark.parametrize("raw, expected", [
    ("+", "+"),
    ("Ctrl++", "ctrl++"),
    ("Ctrl+=", None),
])
def test_plus_key_normalization(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            normalize_key(raw)
        return
    assert normalize_key(raw) == expected
