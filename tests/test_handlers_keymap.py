#
"""``keymap.slots`` 内省命令的行为回归（命令层，不启动 GUI）。

用**真实** ``ShortcutRegistry`` / ``ShortcutSlot`` 装配，验证：

* 槽的 id / help / 兜底键位确实从注册表读出；
* 一键多命令的冲突对进入 ``conflicts``（table 追加段落 + JSON 字段）；
* 注册表没进容器 ≠ 空注册表，两者都不抛异常（退出码 0）。
"""

from __future__ import annotations

import json

from albuswall.command import CommandRegistry
from albuswall.core import Container
from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.command.handlers import keymap as keymap_cmd
from albuswall.ui.keymap import ShortcutRegistry
from albuswall.ui.keymap.model import ShortcutSlot


def _dispatch(
        container: Container,
        path: str = "keymap.slots",
        args: list | tuple = (),
        kwargs: dict | None = None,
):
    registry = CommandRegistry()
    keymap_cmd.setup(registry, container)
    return Dispatcher(registry).dispatch(path, list(args), dict(kwargs or {}))


def _container(shortcuts: ShortcutRegistry | None) -> Container:
    container = Container()
    if shortcuts is not None:
        container.set("shortcuts", shortcuts)
    return container


def _registry() -> ShortcutRegistry:
    shortcuts = ShortcutRegistry()
    shortcuts.register_slot(ShortcutSlot(
        id="global.save",
        handler=lambda: None,
        help="Save current album",
        default_keys=("Ctrl+S",),
    ))
    shortcuts.register_slot(ShortcutSlot(
        id="edit.save",
        handler=lambda: None,
        help="Save the editor",
        default_keys=("Ctrl+Shift+S",),
    ))
    # 一键多命令：Ctrl+S 同时绑到两条命令 → conflicts 里应出现一次
    shortcuts.bind("Ctrl+S", "global.save", "edit.save")
    return shortcuts


# ==================== happy path ====================

def test_slots_table_lists_slots_in_registration_order():
    shortcuts = _registry()
    table = _dispatch(_container(shortcuts), "keymap.slots")

    ids = [slot.id for slot in shortcuts.slots()]
    positions = [table.index(slot_id) for slot_id in ids]
    assert positions == sorted(positions)
    assert "Ctrl+Shift+S" in table
    assert "Save current album" in table


def test_slots_json_schema():
    payload = _dispatch(_container(_registry()), "keymap.slots",
                        kwargs={"format": "json"})
    assert json.loads(json.dumps(payload))  # 可 JSON 往返
    by_id = {item["id"]: item for item in payload["slots"]}
    assert by_id["global.save"]["help"] == "Save current album"
    assert by_id["global.save"]["default_keys"] == ["Ctrl+S"]


def test_slots_reports_conflicts():
    payload = _dispatch(_container(_registry()), "keymap.slots",
                        kwargs={"format": "json"})
    (conflict,) = payload["conflicts"]
    assert conflict["keys"] == ["Ctrl+S"]
    assert conflict["slot_ids"] == ["global.save", "edit.save"]

    table = _dispatch(_container(_registry()), "keymap.slots")
    assert "conflicts" in table
    assert "global.save" in table.split("conflicts", 1)[1]


def test_slots_table_without_conflicts_says_none():
    shortcuts = ShortcutRegistry()
    shortcuts.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: None, default_keys=("Ctrl+Q",)))
    table = _dispatch(_container(shortcuts), "keymap.slots")
    assert "conflicts: none" in table


def test_slots_verbose_explains_sources():
    table = _dispatch(_container(_registry()), "keymap.slots",
                      kwargs={"verbose": True})
    assert "default_keys 来自 ShortcutSlot" in table.split("sources:", 1)[1]


def test_slots_accepts_space_separated_format():
    payload = _dispatch(_container(_registry()), "keymap.slots",
                        args=["json"], kwargs={"format": True})
    assert "slots" in payload


# ==================== 空 / 未接线 ====================

def test_slots_empty_registry():
    out = _dispatch(_container(ShortcutRegistry()), "keymap.slots")
    assert out == "no slots registered"


def test_slots_when_registry_not_wired():
    out = _dispatch(_container(None), "keymap.slots")
    assert "not registered in this process" in out


# ==================== 命名空间 / 只读 ====================

def test_namespace_forwards_to_slots():
    table = _dispatch(_container(_registry()), "keymap", args=["slots"])
    assert "global.save" in table


def test_namespace_without_subcommand_prints_usage():
    usage = _dispatch(_container(_registry()), "keymap")
    assert "子命令" in usage and "slots" in usage


def test_commands_read_only_and_idempotent():
    shortcuts = _registry()
    before = (len(shortcuts.slots()), dict(shortcuts.conflicts()))
    container = _container(shortcuts)
    _dispatch(container, "keymap.slots", kwargs={"format": "json"})
    _dispatch(container, "keymap.slots")
    after = (len(shortcuts.slots()), dict(shortcuts.conflicts()))
    assert before == after
