#
"""DispatchPolicy：四种 Qt 状态 × 各动作配置，规则只此一处。"""
from __future__ import annotations

import pytest

from albuswall.ui.builtin.keymap.dispatch import DispatchPolicy
from albuswall.ui.builtin.keymap.model import Action, Context, Scope


def _action(action_id="demo.a", *, scope=Scope.COMPONENT, when=None,
            allow_in_text_input=False) -> Action:
    return Action(
        id=action_id, label=action_id, description="demo",
        scope=scope, when=when, allow_in_text_input=allow_in_text_input,
        handler=lambda ctx: True, signature=None, takes_context=True,
    )


def test_component_action_needs_focus():
    action = _action()
    assert DispatchPolicy(focus_owned=True).allows(action, Context())
    assert not DispatchPolicy(focus_owned=False).allows(action, Context())


def test_global_action_works_without_focus():
    action = _action(scope=Scope.GLOBAL)
    assert DispatchPolicy(focus_owned=False).allows(action, Context())


def test_text_input_blocks_component_actions_unless_allowed():
    normal = _action()
    allowed = _action(allow_in_text_input=True)
    global_action = _action(scope=Scope.GLOBAL)
    policy = DispatchPolicy(in_text_input=True)
    assert not policy.allows(normal, Context())
    assert policy.allows(allowed, Context())
    assert policy.allows(global_action, Context())


def test_when_predicate_is_the_only_availability_rule():
    ctx = Context(window=object())
    seen = []

    def when(inner):
        seen.append(inner)
        return False

    action = _action(when=when)
    assert not DispatchPolicy().allows(action, ctx)
    assert seen == [ctx]


def test_when_exception_skips_only_that_action():
    def boom(_ctx):
        raise RuntimeError("nope")

    assert not DispatchPolicy().allows(_action(when=boom), Context())


def test_modal_dialog_yields_all_actions():
    action = _action()
    policy = DispatchPolicy(focus_owned=True, modal_open=True)
    assert policy.allows(action, Context()) is False


def test_key_recording_yields_all_actions():
    action = _action()
    policy = DispatchPolicy(focus_owned=True, editing_sequence=True)
    assert policy.allows(action, Context()) is False


@pytest.mark.parametrize("focus_owned,in_text_input", [
    (True, False), (True, True), (False, False), (False, True),
])
def test_four_input_combinations(focus_owned, in_text_input):
    policy = DispatchPolicy(focus_owned=focus_owned, in_text_input=in_text_input)
    normal = policy.allows(_action(), Context())
    allowed = policy.allows(_action(allow_in_text_input=True), Context())
    global_action = policy.allows(_action(scope=Scope.GLOBAL), Context())
    assert allowed is focus_owned
    assert global_action is True
    assert normal is (focus_owned and not in_text_input)
