#
"""值的唯一中枢：惰性 + 记忆化 + 类型转换 + 环检测 + origin 追踪。

除取值之外，Resolver 还维护两份「来源」信息，供回写（flush）使用：

- ``_origins``: node -> 生效值来自哪个文件（``origin_of``）
- ``_definitions``: node -> 所有定义过它的文件（低 → 高优先级，``definitions_of``）

以及一份脏集合 ``_dirty``（``set`` 缺省只标脏，不落盘）。

线程安全：所有读写共享状态的方法都在同一把 ``RLock`` 下执行；
``RLock`` 是必需的——``get_str`` 展开 ``${...}`` 时会递归调用自身。
"""

import re
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from configparser import InterpolationError

# noinspection protected-member
from .utils.ini_parser import _convert  # 复用现有转换器

REF_RE = re.compile(r'\${([^}:]+)(?::([^}]+))?}')

Node = tuple[str, str]


class Resolver:
    """惰性 + 记忆化 + 环检测 + 类型转换。所有访问路径的唯一中枢。"""

    def __init__(self):
        self._raw: dict[Node, Any] = {}
        self._types: dict[Node, Any] = {}
        self._custom_converters: dict = {}

        self._str_cache: dict[Node, Any] = {}  # 展开后的字符串 / 原始值
        self._typed_cache: dict[Node, Any] = {}  # 类型化值

        self._stack: list[Node] = []
        self._rdeps: dict[Node, set[Node]] = {}

        # ── 来源追踪（阶段 1）──────────────────────────────
        self._origins: dict[Node, Path] = {}  # node -> 生效值所在文件
        self._by_file: dict[Path, set[Node]] = {}  # 反查，flush 时按文件分组
        self._definitions: dict[Node, list[Path]] = {}  # 低 → 高，允许重复
        self._readonly_files: set[Path] = set()  # 显式标记的只读来源
        self._dirty: set[Node] = set()  # 待回写节点
        self._lock = threading.RLock()

    # ---------- 字符串展开 ----------
    def get_str(self, section: str, key: str) -> Any:
        node = (section, key)
        with self._lock:
            return self._get_str_locked(node)

    def _get_str_locked(self, node: Node) -> Any:
        if node not in self._raw:
            raise KeyError(f"[{node[0]}]{node[1]}")

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
                node[1], node[0],
                "循环引用: " + " -> ".join(f"[{s}]{k}" for s, k in chain),
            )

        self._stack.append(node)
        section = node[0]
        try:
            def replace(m):
                ref_sec = m.group(1) if m.group(2) else section
                ref_key = m.group(2) or m.group(1)
                self._rdeps.setdefault((ref_sec, ref_key), set()).add(node)
                # 回调必须返回 str
                return str(self._get_str_locked((ref_sec, ref_key)))

            self._str_cache[node] = REF_RE.sub(replace, raw)
            return self._str_cache[node]
        finally:
            self._stack.pop()

    # ---------- 类型化 ----------
    def get_typed(self, section: str, key: str) -> Any:
        node = (section, key)
        with self._lock:
            if node in self._typed_cache:
                return self._typed_cache[node]

            raw = self._get_str_locked(node)
            converter = self._types.get(node)

            if converter is None or not isinstance(raw, str):
                # 无转换器，或 raw 已是具体值 → 直接采用，不再过 _convert
                value = raw
            else:
                value = _convert(raw, converter, self._custom_converters)

            self._typed_cache[node] = value
            return value

    # ---------- 写 + 失效 ----------
    def set(self, section: str, key: str, value, *, persist: bool = True) -> None:
        """写入运行期值。

        Args:
            persist: True（默认）时把节点标脏，等待 ``flush`` 落盘；
                False 时只改内存，不参与回写。
        """
        node = (section, key)
        with self._lock:
            self._raw[node] = value
            if persist:
                self._dirty.add(node)
            self._invalidate(node)

    def inject_raw(self, section, key, raw, *, origin: Optional[Path] = None,
                   overwrite: bool = False) -> None:
        """加载阶段注入原始值。

        Args:
            origin: 该值的来源文件；非空时记录 ``origin_of`` 与 ``nodes_of_file``，
                并按「低 → 高优先级」追加到 ``definitions_of``。
            overwrite: 为 True 时覆盖已存在的节点；否则保留先注入的值
                （但 origin 仍会被记录为定义来源，仅在值确实被采用时更新生效 origin）。
        """
        node = (section, key)
        with self._lock:
            if origin is not None:
                origin = Path(origin)
                self._definitions.setdefault(node, []).append(origin)
            if overwrite or node not in self._raw:
                self._raw[node] = raw
                if origin is not None:
                    self.set_origin(section, key, origin)
                self._invalidate(node)

    def _invalidate(self, node) -> None:
        with self._lock:
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

    # ---------- origin / dirty（阶段 1）----------
    @property
    def lock(self) -> threading.RLock:
        """共享状态锁。调用方需要「读-改-写」原子性时用它。"""
        return self._lock

    def origin_of(self, section: str, key: str) -> Optional[Path]:
        """返回该节点生效值所在文件；未追踪到来源时返回 None。"""
        with self._lock:
            return self._origins.get((section, key))

    def set_origin(self, section: str, key: str, origin: Path) -> None:
        """登记节点生效值的来源文件（运行期 ``set(..., target=...)`` 使用）。"""
        node = (section, key)
        with self._lock:
            origin = Path(origin)
            old = self._origins.get(node)
            if old is not None and old != origin:
                bucket = self._by_file.get(old)
                if bucket is not None:
                    bucket.discard(node)
                    if not bucket:
                        self._by_file.pop(old, None)
            self._origins[node] = origin
            self._by_file.setdefault(origin, set()).add(node)

    def nodes_of_file(self, path: Path) -> set[Node]:
        """返回「生效值来自该文件」的节点集合（副本）。"""
        with self._lock:
            return set(self._by_file.get(Path(path), ()))

    def definitions_of(self, section: str, key: str) -> tuple[Path, ...]:
        """返回定义过该节点的所有文件，按注入顺序（低 → 高优先级）。"""
        with self._lock:
            return tuple(self._definitions.get((section, key), ()))

    def mark_readonly_file(self, path: Path) -> None:
        """标记来源文件为只读，``flush`` 不会写入它。"""
        with self._lock:
            self._readonly_files.add(Path(path))

    def is_readonly_file(self, path: Path) -> bool:
        """来源文件是否被标记为只读（不看文件系统权限）。"""
        with self._lock:
            return Path(path) in self._readonly_files

    def dirty_nodes(self) -> frozenset[Node]:
        """返回待回写节点（副本）。"""
        with self._lock:
            return frozenset(self._dirty)

    def mark_clean(self, nodes: Iterable[Node]) -> None:
        """把节点从脏集合移除（``flush`` 成功后调用）。"""
        with self._lock:
            for node in nodes:
                self._dirty.discard(tuple(node))

    def converter_of(self, section: str, key: str) -> Any:
        """返回节点注册的转换器；未注册时返回 None。"""
        with self._lock:
            return self._types.get((section, key))

    # ---------- 查询辅助 ----------
    def is_cached(self, section, key) -> bool:
        with self._lock:
            return (section, key) in self._typed_cache  # 报「已类型化」为准

    def __contains__(self, node) -> bool:
        with self._lock:
            return node in self._raw

    def nodes(self) -> Iterator[Node]:
        with self._lock:
            return iter(tuple(self._raw))

    def sections(self):
        with self._lock:
            return sorted({s for s, _ in self._raw})

    def keys_of(self, section):
        with self._lock:
            return sorted(k for s, k in self._raw if s == section)

    # ---------- 注册（加载阶段调用）----------
    def register_type(self, section, key, converter):
        with self._lock:
            self._types[(section, key)] = converter

    def register_custom_converters(self, mapping):
        with self._lock:
            self._custom_converters = dict(mapping)

    # ---------- 查询 ----------
    def has_section(self, section: str) -> bool:
        with self._lock:
            return any(s == section for s, _ in self._raw)

    # ---------- 自检 ----------
    def check_all(self) -> None:
        with self._lock:
            for node in tuple(self._raw):
                self.get_typed(*node)

    #
    def types_of(self, section: str) -> dict[str, Any]:
        """返回 [{key: converter}]，供 UI / 校验 / 文档使用。"""
        with self._lock:
            return {
                k: self._types[(s, k)]
                for s, k in tuple(self._types) if s == section
            }

    def schema(self) -> dict[str, dict[str, Any]]:
        """返回 {section: {key: converter}}，跳过 __default__。"""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for (s, k), conv in tuple(self._types.items()):
                if s == "__default__":
                    continue
                out.setdefault(s, {})[k] = conv
        return out
