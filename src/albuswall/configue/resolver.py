#
""""""

import re
from typing import Any
from configparser import InterpolationError

# noinspection protected-member
from .utils.ini_parser import _convert  # 复用现有转换器

REF_RE = re.compile(r'\${([^}:]+)(?::([^}]+))?}')


class Resolver:
    """惰性 + 记忆化 + 环检测 + 类型转换。所有访问路径的唯一中枢。"""

    def __init__(self):
        self._raw: dict[tuple[str, str], Any] = {}
        self._types: dict[tuple[str, str], Any] = {}
        self._custom_converters: dict = {}

        self._str_cache: dict[tuple[str, str], Any] = {}  # 展开后的字符串 / 原始值
        self._typed_cache: dict[tuple[str, str], Any] = {}  # 类型化值

        self._stack: list[tuple[str, str]] = []
        self._rdeps: dict[tuple[str, str], set[tuple[str, str]]] = {}

    # ---------- 字符串展开 ----------
    def get_str(self, section: str, key: str) -> Any:
        node = (section, key)
        if node not in self._raw:
            raise KeyError(f"[{section}]{key}")

        raw = self._raw[node]
        # 非字符串：直接返回，不进展开器（Path / dict / bool / int / None ...）
        if not isinstance(raw, str):
            return raw

        if node in self._str_cache:
            return self._str_cache[node]
        if node in self._stack:
            idx = self._stack.index(node)
            chain = self._stack[idx:] + [node]
            raise InterpolationError(
                key, section,
                "循环引用: " + " -> ".join(f"[{s}]{k}" for s, k in chain),
            )

        self._stack.append(node)
        try:
            def replace(m):
                ref_sec = m.group(1) if m.group(2) else section
                ref_key = m.group(2) or m.group(1)
                self._rdeps.setdefault((ref_sec, ref_key), set()).add(node)
                return str(self.get_str(ref_sec, ref_key))  # 回调必须返回 str

            self._str_cache[node] = REF_RE.sub(replace, raw)
            return self._str_cache[node]
        finally:
            self._stack.pop()

    # ---------- 类型化 ----------
    def get_typed(self, section: str, key: str) -> Any:
        node = (section, key)
        if node in self._typed_cache:
            return self._typed_cache[node]

        raw = self.get_str(section, key)
        converter = self._types.get(node)

        if converter is None or not isinstance(raw, str):
            # 无转换器，或 raw 已是具体值 → 直接采用，不再过 _convert
            value = raw
        else:
            value = _convert(raw, converter, self._custom_converters)

        self._typed_cache[node] = value
        return value

    # ---------- 写 + 失效 ----------
    def set(self, section: str, key: str, value) -> None:
        node = (section, key)
        self._raw[node] = value
        self._invalidate(node)

    def inject_raw(self, section, key, raw, *, overwrite=False):
        node = (section, key)
        if overwrite or node not in self._raw:
            self._raw[node] = raw
            self._invalidate(node)

    def _invalidate(self, node) -> None:
        seen = {node}
        frontier = [node]
        while frontier:
            cur = frontier.pop()
            for dep in self._rdeps.get(cur, ()):
                if dep not in seen:
                    seen.add(dep)
                    frontier.append(dep)
        for n in seen:
            self._str_cache.pop(n, None)
            self._typed_cache.pop(n, None)
            self._rdeps.pop(n, None)

    # ---------- 查询辅助 ----------
    def is_cached(self, section, key) -> bool:
        return (section, key) in self._typed_cache  # 报「已类型化」为准

    def __contains__(self, node) -> bool:
        return node in self._raw

    def nodes(self):
        return iter(self._raw)

    def sections(self):
        return sorted({s for s, _ in self._raw})

    def keys_of(self, section):
        return sorted(k for s, k in self._raw if s == section)

    # ---------- 注册（加载阶段调用） ----------
    def register_type(self, section, key, converter):
        self._types[(section, key)] = converter

    def register_custom_converters(self, mapping):
        self._custom_converters = dict(mapping)

    # ---------- 查询 ----------
    def has_section(self, section: str) -> bool:
        return any(s == section for s, _ in self._raw)

    # ---------- 自检 ----------
    def check_all(self) -> None:
        for node in list(self._raw):
            self.get_typed(*node)

    #
    def types_of(self, section: str) -> dict[str, Any]:
        """返回 [{key: converter}]，供 UI / 校验 / 文档使用。"""
        return {k: self._types[(s, k)] for s, k in self._types if s == section}

    def schema(self) -> dict[str, dict[str, Any]]:
        """返回 {section: {key: converter}}，跳过 __default__。"""
        out: dict[str, dict[str, Any]] = {}
        for (s, k), conv in self._types.items():
            if s == "__default__":
                continue
            out.setdefault(s, {})[k] = conv
        return out
