"""内置动作表：全部动作可绑定、默认键位齐全、系统键不被占用。"""
from __future__ import annotations

import pytest

from albuswall.ui.builtin import actions as builtin_actions
from albuswall.ui.builtin import shortcuts as builtin_shortcuts
from albuswall.ui.builtin.keymap import registry
from albuswall.ui.builtin.keymap.bindings import normalize_key
from albuswall.ui.builtin.keymap.policy import RESERVED_KEYS, validate_binding


@pytest.fixture(scope="module", autouse=True)
def _load_actions():
    # 导入即注册；显式 import 让 pytest 能收集到动作表
    assert builtin_shortcuts.BUILTIN_ACTION_IDS
    assert builtin_actions.action_ids()


@pytest.mark.parametrize("action_id", [
    *builtin_shortcuts.BUILTIN_ACTION_IDS,
    *builtin_actions.action_ids(),
])
def test_every_declared_action_is_registered(action_id):
    action = registry.get_action(action_id)
    assert action is not None, f"{action_id} is not registered"
    assert action.description


def test_expected_default_keys():
    expected = {
        "delete": "asset.trash",
        "ctrl+shift+delete": "asset.empty_trash",
        "m": "asset.move_target",
        "ctrl+shift+m": "asset.move_to_album_picker",
        "ctrl+z": "asset.undo",
        "ctrl+shift+z": "asset.redo",
        "ctrl+c": "asset.copy",
        "ctrl+x": "asset.cut",
        "ctrl+v": "asset.paste",
        "f2": "asset.rename",
        "ctrl+e": "asset.export",
        "f11": "window.fullscreen",
        "ctrl+0": "viewer.zoom_fit",
        "ctrl+1": "viewer.zoom_actual",
        "space": "preview.space",
        "s": "preview.slideshow",
        "f5": "preview.slideshow",
        "k": "preview.play_pause",
        "e": "viewer.edit",
        "t": "viewer.tag_focus",
        "shift+s": "viewer.share",
        "ctrl+alt+r": "asset.reveal",
        "ctrl+alt+c": "asset.copy_path",
        "ctrl+shift+n": "asset.new_album",
        "f3": "search.next",
        "shift+f3": "search.previous",
        "ctrl+shift+f": "filter.advanced",
        "ctrl+shift+l": "filter.clear",
    }
    for key, action_id in expected.items():
        ops = [binding.action for binding in registry.effective_bindings()[key]]
        assert action_id in ops, f"{key} should run {action_id}, got {ops}"


def test_shift_digit_slots_carry_their_slot_argument():
    for index in range(1, 10):
        key = f"shift+{index}"
        binding = registry.effective_bindings()[key][0]
        assert binding.action == "asset.move_to_slot"
        assert dict(binding.args) == {"slot": index}


def test_rating_keys_carry_their_value():
    for value in range(6):
        binding = registry.effective_bindings()[str(value)][0]
        assert binding.action == "asset.rating"
        assert dict(binding.args) == {"rating": value}


def test_reserved_keys_only_host_system_actions():
    for key in RESERVED_KEYS:
        ops = registry.effective_bindings()[key]
        for binding in ops:
            validate_binding(key, binding.action)


def test_all_default_keys_are_normalized():
    for key in registry.effective_bindings():
        assert normalize_key(key) == key
