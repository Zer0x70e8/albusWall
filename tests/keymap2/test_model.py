#
"""内核：注册 / 去重 / when / Scope / seq / 组件树。"""
from __future__ import annotations

import pytest

from albuswall.ui.builtin.keymap.components import (
    COMPONENTS,
    clear_components,
    component_path,
    register_component,
)
from albuswall.ui.builtin.keymap import model as model_module
from albuswall.ui.builtin.keymap.model import (
    Action,
    Binding,
    Component,
    Registry,
    Scope,
    shortcut,
)


def _action(action_id: str, keys=(), **kwargs) -> Action:
    def handler(ctx):
        return True

    return Action(
        id=action_id, label=action_id, description="demo",
        keys=tuple(keys), handler=handler, signature=None,
        takes_context=True, **kwargs,
    )


def test_register_and_duplicate():
    store = Registry()
    store.register(_action("demo.a", ("ctrl+s",)))
    with pytest.raises(ValueError):
        store.register(_action("demo.a", ("ctrl+t",)))
    assert store.get_action("demo.a").id == "demo.a"
    store.unregister("demo.a")
    assert store.get_action("demo.a") is None


def test_decorator_requires_description(monkeypatch):
    store = Registry()
    monkeypatch.setattr(model_module, "registry", store)
    with pytest.raises(ValueError):
        @shortcut("demo.nodesc")
        def no_doc(ctx):
            return None

    @shortcut("demo.doc", description="from docstring")
    def with_doc(ctx):
        """Docstring first paragraph."""
        return None

    action = store.get_action("demo.doc")
    assert action is not None
    assert action.description == "from docstring"


def test_docstring_fallback(monkeypatch):
    store = Registry()
    monkeypatch.setattr(model_module, "registry", store)
    @shortcut("demo.fallback")
    def documented(ctx):
        """First line.

        More details that must be ignored.
        """
        return None

    assert store.get_action("demo.fallback").description == "First line."


def test_scope_and_keys_normalized(monkeypatch):
    store = Registry()
    monkeypatch.setattr(model_module, "registry", store)

    @shortcut("demo.global", keys=["CTRL + Alt + q", "F5"], description="d",
              scope="global")
    def global_action(ctx):
        return None

    action = store.get_action("demo.global")
    assert action.scope is Scope.GLOBAL
    assert action.is_global
    assert action.keys == ("ctrl+alt+q", "f5")


def test_seq_decides_execution_order():
    store = Registry()
    first = store.register(_action("demo.first", ("ctrl+x",)))
    second = store.register(_action("demo.second", ("ctrl+x",)))
    assert first.seq < second.seq
    assert [a.id for a, _b in store.bindings("ctrl+x")] == [
        "demo.first", "demo.second"]


def test_components_are_static_tree():
    assert any(c.id == "window.detail" for c in COMPONENTS)
    path = component_path("window.detail.tagbar")
    assert [c.id for c in path] == ["window", "window.detail", "window.detail.tagbar"]
    extra = Component("plugin.card", "Plugin card", "app", "", 1)
    try:
        register_component(extra)
        assert component_path("plugin.card")[-1].label == "Plugin card"
    finally:
        clear_components()
    assert not any(c.id == "plugin.card" for c in COMPONENTS)


def test_binding_args_frozen_and_sorted():
    binding = Binding.build("demo.a", {"z": 1, "a": "x"})
    assert binding.args == (("a", "x"), ("z", 1))
    assert binding.arg_map == {"a": "x", "z": 1}
    assert Binding.from_raw({"action": "demo.a", "args": {"a": 1}}) == Binding.build(
        "demo.a", {"a": 1})


def test_package_imports_without_qt():
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import sys
class BlockQt:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "PySide6":
            raise ImportError("blocked")
        return None
sys.meta_path.insert(0, BlockQt())
import albuswall.ui.builtin.keymap as km
assert km.SECTION == "shortcuts"
assert "PySide6" not in sys.modules
print("OK")
"""
    src = Path(__file__).resolve().parents[2] / "src"
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env={"PYTHONPATH": str(src)}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_dispatch_runs_multiple_actions_in_seq_order():
    from albuswall.ui.builtin.keymap.dispatch import DispatchPolicy
    from albuswall.ui.builtin.keymap.model import Context

    calls: list[str] = []

    def make(action_id: str):
        def handler(ctx):
            calls.append(action_id)
            return True

        return Action(
            id=action_id, label=action_id, description="demo",
            keys=("ctrl+x",), handler=handler, signature=None,
            takes_context=True,
        )

    store = Registry()
    store.register(make("demo.second"))
    store.register(make("demo.first"))
    policy = DispatchPolicy()
    assert store.dispatch("ctrl+x", Context(), lambda action: policy.allows(
        action, Context())) is True
    assert calls == ["demo.second", "demo.first"]
