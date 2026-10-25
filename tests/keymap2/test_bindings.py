#
"""绑定层：单账合并 / 与默认值比较 / 旧格式迁移。"""
from __future__ import annotations

import inspect

from albuswall.configue.resolver import Resolver
from albuswall.ui.builtin.keymap import config
from albuswall.ui.builtin.keymap.bindings import (
    default_bindings,
    merge_bindings,
    normalize_key,
    render_ops,
)
from albuswall.ui.builtin.keymap.model import Action, Binding, Registry
from albuswall.ui.builtin.keymap.tools.migrate_keymap import (
    build_bindings_from_legacy,
)


class FakeConfig:
    """只实现 config.save_to_config 用到的接口（Resolver 类型化照样生效）。"""

    def __init__(self) -> None:
        self.resolver = Resolver()
        self.written: dict[str, list] = {}
        self.removed: list[str] = []
        self.flushed: list[tuple[str, str]] = []

    def set(self, section, key, value, *, save="defer"):
        self.written[key] = value
        self.resolver.set(section, key, value)

    def remove(self, section, key, *, save="defer"):
        self.removed.append(key)
        self.resolver.remove(section, key)

    def flush(self, only=None):
        self.flushed.extend(only or ())


def _action(action_id: str, keys=(), args=None, params=()) -> Action:
    def handler(ctx, **kwargs):
        return True

    signature = inspect.Signature([
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        *(inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            default=default) for name, default in params),
    ])
    return Action(
        id=action_id, label=action_id, description="demo",
        keys=tuple(normalize_key(k) for k in keys), handler=handler,
        arguments=tuple(sorted((args or {}).items())), signature=signature,
        takes_context=True, seq=0,
    )


def _store() -> Registry:
    store = Registry()
    store.register(_action("demo.a", ("Ctrl+S",)))
    store.register(_action("demo.b", ("Ctrl+T",)))
    store.register(_action("demo.page", (), args={"page": ""}, params=(("page", ""),)))
    return store


def test_normalize_key_variants():
    assert normalize_key("CTRL + Shift + o") == "ctrl+shift+o"
    assert normalize_key("left") == "left"
    assert normalize_key("PageUp") == "pgup"
    assert normalize_key("F1") == "f1"
    assert normalize_key("Ctrl+,") == "ctrl+,"
    assert normalize_key("Ctrl++") == "ctrl++"


def test_default_bindings_carry_declared_arguments():
    table = default_bindings([_action("demo.page", ("Ctrl+P",), {"page": "x"})])
    assert table == {"ctrl+p": (Binding("demo.page", (("page", "x"),)),)}


def test_merge_bindings_single_account():
    defaults = {"ctrl+s": (Binding("demo.a"),)}
    assert merge_bindings(defaults, {}) == defaults
    assert merge_bindings(defaults, {"ctrl+s": ()}) == {}
    assert merge_bindings(defaults, {"ctrl+q": (Binding("demo.b"),)}) == {
        "ctrl+s": (Binding("demo.a"),),
        "ctrl+q": (Binding("demo.b"),),
    }


def test_save_writes_only_changes_and_drops_defaults():
    store = _store()
    fake = FakeConfig()
    # 全部默认：不写任何行
    config.save_to_config(fake, store=store)
    assert fake.written == {} and fake.removed == []

    # 同一个键追加操作 → 写完整最终值（单账）
    store.add_operation("ctrl+s", "demo.b")
    config.save_to_config(fake, store=store)
    assert fake.written == {"ctrl+s": ["demo.a", "demo.b"]}
    assert "ctrl+t" not in fake.written

    # 恢复默认 → 删掉那一行
    store.reset_all()
    config.save_to_config(fake, store=store)
    assert fake.removed == ["ctrl+s"]


def test_save_and_load_parameterized_binding():
    store = _store()
    store.add_operation("ctrl+p", "demo.page", {"page": "settings:keybindings"})
    fake = FakeConfig()
    config.save_to_config(fake, store=store)
    assert fake.written["ctrl+p"] == [
        {"action": "demo.page", "args": {"page": "settings:keybindings"}}]

    loaded = config.read_bindings(fake)
    assert loaded["ctrl+p"] == (
        Binding("demo.page", (("page", "settings:keybindings"),)),)


def test_render_ops_roundtrip_json_array():
    import json

    data = json.loads(render_ops([Binding("demo.a"), Binding("demo.page", (("page", "x"),))]))
    assert data == ["demo.a", {"action": "demo.page", "args": {"page": "x"}}]


def test_migrate_format2_additions_and_removals():
    defaults = {"ctrl+s": (Binding("demo.a"),),
                "right": (Binding("viewer.next"),)}
    payload = {
        "format": 2,
        "additions": {
            "Ctrl+Q": [{"action": "demo.page", "args": {"page": "x"}}],
        },
        "removals": {"Right": ["viewer.next"]},
    }
    table = build_bindings_from_legacy(payload, defaults)
    assert table == {"ctrl+s": (Binding("demo.a"),),
                     "right": (),
                     "ctrl+q": (Binding("demo.page", (("page", "x"),)),)}


def test_migrate_format1_bindings():
    defaults = {"ctrl+s": (Binding("demo.a"),),
                "ctrl+t": (Binding("demo.b"),)}
    payload = {"format": 1, "bindings": {"demo.a": "Ctrl+Shift+S"}}
    table = build_bindings_from_legacy(payload, defaults)
    assert table == {
        "ctrl+s": (),
        "ctrl+shift+s": (Binding("demo.a"),),
        "ctrl+t": (Binding("demo.b"),),
    }
