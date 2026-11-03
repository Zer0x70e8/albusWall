#
"""快捷键层内省命令：``keymap.slots``（只读）。

打印运行实例里登记过的所有 ``ShortcutSlot``：id / help / 兜底键位，并顺带
给出「同一组键位绑了多条命令」的冲突对。

初始化契约
----------
keymap 注册表由 UI 启动流程经 ``albuswall.ui.keymap.registry_shortcut_registry``
写进容器的 ``"shortcuts"`` 键，晚于命令装配——所以这里**每个命令体内懒取**，
取不到就提示「本进程没有注册表」并返回（退出码 0）。仅装配（``setup``）不会
往注册表里塞任何东西，``load_shortcuts()`` 的时机仍归调用方。

依赖纪律：只按属性读容器里的对象（鸭子类型），**不 import**
``albuswall.ui.keymap`` —— 命令层不反向依赖 UI 层，也就不引入 Qt。

``pass_raw`` 的理由：自省 / 诊断类命令，且 CLI 的 ``--format json`` 写法只有
拿原始 ``(args, kwargs)`` 才收得住。

CLI 示例::

    albusctl keymap slots
    albusctl keymap slots --format=json
    albusctl keymap slots --verbose        # 额外说明字段来源
"""

from __future__ import annotations

from typing import Any

from albuswall.command import CommandRegistry
from albuswall.core import Container

from ._namespace import register_namespace
from ._render import render_table, split_common_args

__all__: list[str] = []

#: 容器里 keymap 注册表的 key（由 ``registry_shortcut_registry`` 写入）。
SHORTCUT_REGISTRY_KEY = "shortcuts"

_NOT_WIRED = (
    "shortcut registry is not registered in this process: nothing called "
    "registry_shortcut_registry(container) yet. Start the GUI (or wire the "
    "container) and retry."
)


def _shortcuts(container: Container) -> Any | None:
    """懒取 keymap 注册表；本进程没有就是 ``None``（不是错误）。"""
    return container.get(SHORTCUT_REGISTRY_KEY)


def _keys_text(keys: Any) -> str:
    """``KeySequence`` 元组 → ``Ctrl+S`` 之类；空元组 → ``-``。"""
    values = [str(k) for k in (keys or ())]
    return ", ".join(values) if values else "-"


def _conflicts(reg: Any) -> list[dict[str, list[str]]]:
    """一键多命令的冲突对（keymap 的 ``conflicts()`` 只返回长度 > 1 的项）。"""
    result: list[dict[str, list[str]]] = []
    for keys, commands in (reg.conflicts() or {}).items():
        slot_ids = [str(c) for c in commands]
        if len(slot_ids) < 2:
            continue
        result.append({"keys": [str(keys)], "slot_ids": slot_ids})
    return result


def setup(registry: CommandRegistry, container: Container) -> None:
    # 不在这里解析 keymap 注册表：它由 UI 启动流程后注册（见模块 docstring）。
    _ = container

    @registry.command(
        "keymap.slots",
        help_="列出快捷键槽与冲突（只读；--format=json 机器可读）",
        params=(),
        pass_raw=True,
    )
    def keymap_slots(args: list, kwargs: dict) -> Any:
        fmt, verbose, _positional = split_common_args(args, kwargs)
        shortcuts = _shortcuts(container)
        if shortcuts is None:
            return _NOT_WIRED

        slots = list(shortcuts.slots())
        if not slots:
            return "no slots registered"

        entries = [
            {
                "id": str(getattr(slot, "id", "?")),
                "help": str(getattr(slot, "help", "") or ""),
                "default_keys": [
                    str(k) for k in (getattr(slot, "default_keys", ()) or ())
                ],
            }
            for slot in slots
        ]
        conflicts = _conflicts(shortcuts)

        if fmt == "json":
            return {"slots": entries, "conflicts": conflicts}

        lines = [
            render_table(
                ("id", "help", "default_keys"),
                [
                    [
                        entry["id"],
                        entry["help"],
                        ", ".join(entry["default_keys"])
                        if entry["default_keys"] else "-",
                    ]
                    for entry in entries
                ],
            )
        ]
        if conflicts:
            lines.append("")
            lines.append("conflicts (same keys → several slots):")
            for item in conflicts:
                lines.append(
                    f"  {', '.join(item['keys'])}  →  "
                    f"{', '.join(item['slot_ids'])}"
                )
        else:
            lines.append("")
            lines.append("conflicts: none")
        if verbose:
            lines.append("")
            lines.append(
                "sources: id/help/default_keys 来自 ShortcutSlot；"
                "conflicts 来自 ShortcutRegistry.conflicts()。"
            )
        return "\n".join(lines)

    register_namespace(
        registry,
        "keymap",
        help_="快捷键层内省（slots）",
        hint="全部命令只读；加 --format=json 给脚本用。",
    )
