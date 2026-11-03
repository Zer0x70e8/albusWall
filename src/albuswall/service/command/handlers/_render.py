#
"""只读内省命令的渲染工具：表格文本 / JSON 共用的一小层。

只做三件事：

1. **把 CLI 的宽松写法归一**：``--format=json`` 是文档写法，但
   ``parse_cli_args`` 会把它解析成 ``kwargs["format"] == True`` 加一个位置参数
   ``"json"``（见 ``albuswall/cli.py`` 的参数规则）。``split_common_args``
   两种写法都吃，省得每条命令各写一遍；
2. **表格对齐**：CJK 字符占两列宽，按 ``len()`` 补空格会歪，用
   :func:`_display_width` 算显示宽度；
3. **布尔标记**：``✓`` / ``-``，不依赖颜色 / 终端能力。

本模块**不** import Qt，也不 import service / 容器：它只处理字符串与 dict。
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Sequence

from albuswall.command.exceptions import BadRequest

__all__ = [
    "FORMATS",
    "mark",
    "render_table",
    "split_common_args",
    "take_option",
]

#: ``--format`` 的合法取值（顺序即错误提示里的顺序）。
FORMATS: tuple[str, ...] = ("table", "json")

#: 位置参数里被当成 «格式提示» 的 token（``--format json`` 的写法）。
_FORMAT_HINTS: tuple[str, ...] = FORMATS

_MARK_ON = "✓"
_MARK_OFF = "-"


# ==================== 入参归一 ====================

def split_common_args(
        args: Sequence[Any], kwargs: dict[str, Any],
) -> tuple[str, bool, list[str]]:
    """把原始 ``(args, kwargs)`` 归一成 ``(format, verbose, positionals)``。

    接受的写法（都等价）::

        --format=json            → kwargs["format"] = "json"
        --format json            → kwargs["format"] = True + 位置参数 "json"
        --json                   → kwargs["json"] = True

    :raises BadRequest: ``--format`` 给了不认识的值（退出码 64）。
    """
    fmt = kwargs.get("format")
    verbose = bool(kwargs.get("verbose")) or bool(kwargs.get("v"))
    if kwargs.get("json"):
        fmt = "json"

    positional: list[str] = []
    for token in args:
        text = str(token)
        lowered = text.strip().lower()
        if lowered in _FORMAT_HINTS:
            fmt = lowered
        elif lowered in ("verbose", "-v"):
            verbose = True
        else:
            positional.append(text)

    if fmt is True or fmt is None:
        fmt = "table"
    fmt = str(fmt).strip().lower()
    if fmt not in FORMATS:
        raise BadRequest(
            f"unknown --format {fmt!r}; expected one of {', '.join(FORMATS)}"
        )
    return fmt, verbose, positional


def take_option(
        kwargs: dict[str, Any], positional: list[str], name: str,
) -> str | None:
    """取 ``--name=value``；也接受 ``--name value``（CLI 会拆成 True + 位置参数）。

    :raises BadRequest: ``--name`` 出现在命令行却没有值。
    """
    value = kwargs.get(name)
    if value is True:
        if not positional:
            raise BadRequest(
                f"--{name} requires a value (use --{name}=VALUE)"
            )
        return positional.pop(0)
    if value is None or value is False:
        return None
    return str(value)


# ==================== 渲染 ====================

def mark(flag: Any) -> str:
    """布尔 → ``✓`` / ``-``。"""
    return _MARK_ON if flag else _MARK_OFF


def render_table(
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
) -> str:
    """渲染成等宽文本表；空 ``rows`` 也会给出表头。"""
    head = [str(h) for h in headers]
    body = [[_cell(value) for value in row] for row in rows]
    widths = [_display_width(h) for h in head]
    for row in body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(cell))

    lines = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(head)).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(head))),
    ]
    for row in body:
        lines.append(
            "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row))
            .rstrip()
        )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符（W/F）算 2 列，其余算 1 列。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )
