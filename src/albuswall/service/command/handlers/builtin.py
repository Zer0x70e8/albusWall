#
"""内置命令：诊断/自省类。

由 ``service/command/handlers/__init__.py`` 的 ``MODULES`` 显式装配，
``setup(registry, container)`` 被调用恰好一次。

历史上曾用 import 副作用注册（包加载末尾 ``from . import builtin``），
后改为显式装配以避免注册顺序不可预测（见 ``handlers/__init__.py`` 的
``MODULES`` 注释）。新增业务域同样在 ``MODULES`` 里加一行，**不要**重新
引入 import 副作用。
"""

from __future__ import annotations

from typing import Any

from albuswall.command import Command, CommandRegistry, Param
from albuswall.core import Container

from ..exceptions import UnknownCommand


def setup(registry: CommandRegistry, container: Container) -> None:
    # builtin 不依赖业务 service；container 仅为满足 HandlerModule 契约。
    _ = container

    registry.register(Command(
        path="ping",
        help="探测服务存活（由 IPC 层实现）",
        external=True,
    ))
    registry.register(Command(
        path="version",
        help="显示版本（由 IPC 层实现）",
        external=True,
    ))

    @registry.command(
        "parse",
        help_="回显 CLI 端解析出的 args/kwargs（诊断用）",
        params=(),
    )
    def parse_cmd(*args: Any, **kwargs: Any) -> dict:
        return {"args": list(args), "kwargs": dict(kwargs)}

    @registry.command(
        "help",
        help_="列出命令或查看某命令的详细帮助"
              "（--all 含隐藏，--json 机器可读）",
        params=(),
        pass_raw=True,
        pass_subject=True,
    )
    def help_cmd(args: list, kwargs: dict, *, subject=frozenset()) -> Any:
        reg = registry
        include_hidden = bool(kwargs.get("all")) or ("--all" in args)
        as_json = bool(kwargs.get("json")) or ("--json" in args)
        targets = [
            a for a in args
            if not (isinstance(a, str) and a.startswith("-"))
        ]

        if as_json:
            if not targets:
                return {
                    "groups": reg.by_group(
                        include_hidden=include_hidden, subject=subject,
                    ),
                    "commands": reg.names(
                        include_hidden=include_hidden, subject=subject,
                    ),
                }
            cmd = reg.resolve(targets[0])
            if cmd is None:
                raise UnknownCommand(targets[0])
            return _detail_json(reg, cmd, subject)

        if not targets:
            return _format_list(
                reg, include_hidden=include_hidden, subject=subject,
            )
        cmd = reg.resolve(targets[0])
        if cmd is None:
            raise UnknownCommand(targets[0])
        return _format_detail(reg, cmd, subject=subject)


# ==================== 格式化工具 ====================

def _first_line(s: str) -> str:
    return s.split("\n", 1)[0] if s else ""


def _param_usage(p: Param) -> str:
    if p.kind == "pos":
        return f"<{p.name}>" if p.required else f"[<{p.name}>]"
    if p.kind == "opt":
        s = f"--{p.name} {p.name.upper()}"
        if p.short:
            s = f"-{p.short}, {s}"
        return s if p.required else f"[{s}]"
    s = f"--{p.name}"
    if p.short:
        s = f"-{p.short}, {s}"
    return f"[{s}]"


def _usage_line(cmd: Command) -> str:
    parts = ["albuswall", cmd.path]
    if cmd.pass_raw:
        parts += ["[args...]", "[--key=value ...]"]
    else:
        parts += [_param_usage(p) for p in cmd.params]
    return " ".join(parts)


def _param_table(cmd: Command) -> str:
    if not cmd.params:
        return "  (无)"
    lines: list[str] = []
    for p in cmd.params:
        if p.kind == "pos":
            left = f"<{p.name}>"
        elif p.kind == "opt":
            left = f"--{p.name} {p.name.upper()}"
            if p.short:
                left = f"-{p.short}, {left}"
        else:
            left = f"--{p.name}"
            if p.short:
                left = f"-{p.short}, {left}"

        meta: list[str] = []
        if p.required:
            meta.append("required")
        elif p.kind != "pos" and p.default is not None:
            meta.append(f"默认: {p.default!r}")
        if p.choices:
            meta.append(f"可选: {', '.join(map(str, p.choices))}")
        if p.help:
            meta.append(p.help)
        lines.append(f"  {left:<26} {'  '.join(meta)}".rstrip())
    return "\n".join(lines)


def _format_list(reg, *, include_hidden: bool, subject) -> str:
    by_group = reg.by_group(include_hidden=include_hidden, subject=subject)
    grouped = {p for paths in by_group.values() for p in paths}
    ungrouped = sorted(
        set(reg.names(include_hidden=include_hidden, subject=subject)) - grouped
    )

    def emit(paths: list[str]) -> list[str]:
        if not paths:
            return []
        width = max(len(p) for p in paths) + 2
        out = []
        for p in paths:
            cmd = reg.get(p)
            h = _first_line(cmd.help) if cmd else ""
            extra = (
                f"  (别名: {', '.join(cmd.aliases)})"
                if cmd and cmd.aliases else ""
            )
            out.append(f"  {p:<{width}}{h}{extra}".rstrip())
        return out

    lines: list[str] = []
    if ungrouped:
        lines.append("命令:")
        lines.extend(emit(ungrouped))
    for g, paths in sorted(by_group.items()):
        if paths:
            lines.append(f"\n[{g}]")
            lines.extend(emit(sorted(paths)))
    if not lines:
        return "(无可用命令)"
    return "\n".join(lines)


# ---- 新增：详情视图的机器可读版本 --------------------------------

def _detail_json(reg, cmd: Command, subject) -> dict:
    """``help --json <path>`` 的 payload。

    与 ``_format_detail`` 同源（同一份 ``Command`` + 同一套
    ``check_executable``），只是输出 dict 而非排版字符串。将来
    ``describe`` 命令直接复用本函数。
    """
    verdict = reg.check_executable(cmd.path, subject)
    return {
        "path": cmd.path,
        "help": cmd.help,
        "aliases": list(cmd.aliases),
        "groups": sorted(cmd.groups),
        "enabled": cmd.enabled,
        "hidden": cmd.hidden,
        "executable": verdict.ok,
        "executable_reason": verdict.reason,
        "usage": _usage_line(cmd),
        "params": [
            {
                "name": p.name, "kind": p.kind,
                # 与 Param 构造签名顺序一致：kind 之后、short 之前。
                # 只给类型名（"str" / "int" / "bool"），不给 repr：
                # 字符串表示里带模块路径，GUI/CLI 都只看名字。
                "type": p.type.__name__ if p.type else None,
                "short": p.short,
                "required": p.required, "default": p.default,
                "choices": list(p.choices) if p.choices else None,
                "help": p.help,
            }
            for p in cmd.params
        ],
        "children": sorted(cmd.children),
    }


def _format_detail(reg, cmd: Command, *, subject) -> str:
    verdict = reg.check_executable(cmd.path, subject)
    if verdict.reason == "external":
        status = "external（由外部层实现，命令服务不执行）"
    else:
        status = "enabled" if verdict.ok else f"not executable ({verdict.reason})"
    if cmd.hidden:
        status += ", hidden"

    out = [f"{cmd.path} — {_first_line(cmd.help) or '(无帮助)'}", "", "用法:"]
    out.append(f"  {_usage_line(cmd)}")
    if cmd.aliases:
        out.append(f"别名: {', '.join(cmd.aliases)}")
    if cmd.groups:
        out.append(f"分组: {', '.join(sorted(cmd.groups))}")
    out.append(f"状态: {status}")
    if not cmd.external:
        out += ["", "参数:", _param_table(cmd)]
    if cmd.children:
        out.append("")
        out.append("子命令:")
        for c in sorted(cmd.children):
            sub = reg.get(c)
            out.append(
                f"  {c:<24}{_first_line(sub.help) if sub else ''}".rstrip()
            )
    return "\n".join(out)
