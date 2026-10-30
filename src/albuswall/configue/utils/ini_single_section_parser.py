#
"""解析 INI 文件中的单个 section，返回该 section 下的全部键值对。"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional

__all__ = [
    "parse_section_lines",
    "parse_section_string",
    "parse_section_file",
    "SectionNotFoundError",
    "IniSyntaxError",
]

# [section]            可选的尾部注释
_SECTION_RE = re.compile(r"^\[(?P<name>[^]]*)]\s*(?:[;#].*)?$")  #  ^\[(?P<name>[^\]]*)\]\s*(?:[;#].*)?$

# key = value / key: value（key 不能以空白或分隔符开头）
_OPTION_RE = re.compile(r"^(?P<key>[^=:\s][^=:]*?)\s*(?P<sep>[=:])\s*(?P<value>.*)$")

_COMMENT_CHARS = ";#"
_CONTINUATION_CHARS = (" ", "\t")


class SectionNotFoundError(KeyError):
    """指定的 section 在文件中不存在。"""


class IniSyntaxError(ValueError):
    """INI 文件存在非法行（仅在 strict=True 时抛出）。"""


def parse_section_lines(
        lines: Iterable[str],
        section: str,
        *,
        case_sensitive: bool = True,
        strict: bool = True,
        required: bool = True,
) -> Dict[str, str]:
    """
    从可迭代的文本行中解析出 ``section`` 下的全部键值对。

    参数
    ----
    lines          : 文本行（带或不带换行符均可）。
    section        : 目标 section 名（不含方括号）。
    case_sensitive : section 名是否区分大小写；键名始终按原文保留。
    strict         : True 时遇到非法行抛 IniSyntaxError，False 时静默跳过。
    required       : True 时目标 section 不存在会抛 SectionNotFoundError。

    返回
    ----
    dict[str, str]，保持文件中出现的先后顺序。
    同名键重复出现时，后面的值覆盖前面的值。
    """
    target = section if case_sensitive else section.casefold()

    result: Dict[str, str] = {}
    in_target = False
    found = False
    last_key: Optional[str] = None

    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\r\n")
        stripped = line.strip()

        # ---- 空行 / 整行注释 ----
        if not stripped or stripped[0] in _COMMENT_CHARS:
            continue

        # ---- section 头部 ----
        if stripped.startswith("["):
            m = _SECTION_RE.match(stripped)
            if m is None or not m.group("name").strip():
                if strict:
                    raise IniSyntaxError(f"第 {lineno} 行：非法的 section 头部 {line!r}")
                continue

            if in_target:  # 已经读完了目标 section，遇到下一个 section 就收工
                break

            name = m.group("name").strip()
            key = name if case_sensitive else name.casefold()
            in_target = key == target
            found = found or in_target
            last_key = None
            continue

        # ---- 目标 section 之外的内容直接忽略 ----
        if not in_target:
            continue

        # ---- 缩进续行：接到上一个键的值后面 ----
        if line[:1] in _CONTINUATION_CHARS and last_key is not None:
            old = result[last_key]
            result[last_key] = f"{old} {stripped}".strip()
            continue

        # ---- 普通键值对 ----
        m = _OPTION_RE.match(stripped)
        if m is None:
            if strict:
                raise IniSyntaxError(f"第 {lineno} 行：不是合法的键值对 {line!r}")
            continue

        key = m.group("key").strip()
        value = m.group("value").strip()
        result[key] = value
        last_key = key

    if required and not found:
        raise SectionNotFoundError(section)

    return result


def parse_section_string(text: str, section: str, **kwargs) -> Dict[str, str]:
    """从字符串中解析单个 section。"""
    return parse_section_lines(text.splitlines(), section, **kwargs)


def parse_section_file(
        path,
        section: str,
        *,
        encoding: str = "utf-8-sig",  # utf-8-sig 可以顺带吃掉 BOM
        **kwargs,
) -> Dict[str, str]:
    """从文件路径中解析单个 section。"""
    with open(path, "r", encoding=encoding) as fp:
        return parse_section_lines(fp, section, **kwargs)


# ---------------------------------------------------------------------------
# 自测 / 使用示例
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    DEMO = """\
; 顶部注释
[server]
host = 127.0.0.1
port: 8080
name = My Server          ; 值里的分号会被原样保留
desc = 第一行
       第二行（续行）
# 注释行

[db]
user = root
password = 123456
"""

    print("server ->", parse_section_string(DEMO, "server"))
    print("db     ->", parse_section_string(DEMO, "db"))

    # 不区分大小写
    print("SERVER ->", parse_section_string(DEMO, "SERVER", case_sensitive=False))

    # section 不存在：默认抛异常，required=False 时返回 {}
    try:
        parse_section_string(DEMO, "redis")
    except SectionNotFoundError as exc:
        print("redis  ->", type(exc).__name__, exc)
    print("redis  ->", parse_section_string(DEMO, "redis", required=False))
