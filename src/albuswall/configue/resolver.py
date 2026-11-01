#
"""值的唯一中枢：惰性 + 记忆化 + 类型转换 + 环检测 + origin 追踪。

除取值之外，Resolver 还维护两份「来源」信息，供回写（flush）与卸载（unload）使用：

- ``_origins``: node -> 生效值来自哪个文件（``origin_of``）
- ``_definitions``: node -> 所有定义过它的文件（低 → 高优先级，``definitions_of``）

以及一份脏集合 ``_dirty``（``set`` 缺省只标脏，不落盘）。

线程安全：所有读写共享状态的方法都在同一把 ``RLock`` 下执行；
``RLock`` 是必需的——``get_str`` 展开 ``${...}`` 时会递归调用自身。
"""

from __future__ import annotations

import re
import threading
import types
from collections.abc import Mapping
from configparser import InterpolationError
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Optional,
    Union,
    get_args,
    get_origin,
)

from .utils.ini_parser import _convert  # 复用现有转换器

REF_RE = re.compile(r'\${([^}:]+)(?::([^}]+))?}')

Node = tuple[str, str]

_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))
_CONTAINER_ORIGINS = (list, tuple, set, frozenset)


def _coerce_native(
        raw: Any,
        converter: Any,
        custom_converters: Optional[dict],
) -> Any:
    """把「JSON 原生结构」按声明的泛型逐项转成目标类型。

    ``_convert`` 只吃字符串（INI 的形态）；JSON 后端给的是 list / dict /
    标量，所以这里补一层「容器 + 元素」的递归。规则：

    - 非泛型（``int`` / ``Path`` / 自定义类）→ **不动**：值已经是 typed，
      交给 ``get_typed`` 原样返回（``isinstance`` 校验是写值方的事）
    - ``list[X]`` / ``set[X]`` / ``frozenset[X]`` / ``tuple[X, ...]`` →
      逐项转 ``X``（字符串项走 ``_convert``；类型已匹配则原样）
    - ``dict[str, X]`` → 逐值转 ``X``
    - ``Optional[X]`` / ``Union[...]`` → ``None`` 直接放过，其余不动
      （``Union`` 该选哪支是 ``_convert`` 的判断，这里没有字符串可转）

    这样 ``{"recent": ["/a.png"]}`` + ``list[Path]`` 读出来就是 ``[Path]``，
    与写回侧 ``to_json_native`` 的逐项 ``str`` 对称。
    """
    origin = get_origin(converter)
    if origin is None:
        return raw

    args = get_args(converter)

    if origin in _UNION_ORIGINS:
        return None if raw is None else raw

    if origin is dict and isinstance(raw, Mapping):
        value_type = args[1] if len(args) > 1 else None
        return {
            key: _coerce_native(value, value_type, custom_converters)
            for key, value in raw.items()
        }

    if origin in _CONTAINER_ORIGINS and isinstance(
            raw, (list, tuple, set, frozenset),
    ):
        item_type = args[0] if args else None
        items = [
            _coerce_scalar(item, item_type, custom_converters)
            for item in raw
        ]
        return tuple(items) if origin is tuple else origin(items)

    return raw


def _coerce_scalar(item: Any, type_: Any, custom_converters: Optional[dict]) -> Any:
    """单个元素的转换：能匹配就原样，是字符串就按 ``_convert`` 转。"""
    if type_ is None or type_ is Any:
        return item
    if isinstance(item, str):
        return _convert(item, type_, custom_converters)
    if get_origin(type_) is not None:
        return _coerce_native(item, type_, custom_converters)
    try:
        if isinstance(item, type_):
            return item
        return _convert(str(item), type_, custom_converters)
    except TypeError:  # type_ 不是类（特殊形式）→ fail-open
        return item


def _describe_type(type_: Any) -> str:
    """类型的人类可读名（只用于报错文本，不求完整）。"""
    if type_ is None:
        return "?"
    name = getattr(type_, "__name__", None)
    if isinstance(name, str):
        return name
    return str(type_).replace("typing.", "")


def coerce_value(
        raw: Any,
        type_: Any,
        *,
        custom_converters: Optional[Mapping[Any, Any]] = None,
) -> Any:
    """把「外部输入」（CLI / IPC / GUI 传来的值）按声明类型转换。

    语义与**读配置文件完全一致**——这是硬要求：写回与读取同源，否则
    「GUI 写进去的值重启后读不出来」这类不对称 bug 会长期潜伏。

    - 字符串 → ``utils.ini_parser._convert``（bool / int / float / Enum /
      Path / 泛型容器 / 自定义转换器全走同一套）；
    - 非字符串（JSON 后端的原生结构 / 已类型化的值）→ ``_coerce_native``；
    - ``type_`` 为 ``None`` / ``Any`` → 原样返回。

    Raises:
        ValueError: 转不过去；消息里带原值与目标类型名。
    """
    if type_ is None or type_ is Any:
        return raw

    custom = dict(custom_converters or {})
    try:
        if isinstance(raw, str):
            return _convert(raw, type_, custom)
        return _coerce_native(raw, type_, custom)
    except Exception as exc:  # noqa: BLE001  统一收口成 ValueError
        raise ValueError(
            f"cannot convert {raw!r} to {_describe_type(type_)}: {exc}"
        ) from exc



class Resolver:
    """惰性 + 记忆化 + 环检测 + 类型转换。所有访问路径的唯一中枢。"""

    def __init__(self) -> None:
        self._raw: dict[Node, Any] = {}
        self._types: dict[Node, Any] = {}
        self._custom_converters: dict = {}   # 类型 -> str -> 值（读）
        self._custom_serializers: dict = {}  # 类型 -> 值 -> str（写）

        self._str_cache: dict[Node, Any] = {}  # 展开后的字符串 / 原始值
        self._typed_cache: dict[Node, Any] = {}  # 类型化值

        self._stack: list[Node] = []
        self._rdeps: dict[Node, set[Node]] = {}

        # ── 来源追踪 ───────────────────────────────────────
        self._origins: dict[Node, Path] = {}  # node -> 生效值所在文件
        self._by_file: dict[Path, set[Node]] = {}  # 反查，flush 时按文件分组
        self._definitions: dict[Node, list[Path]] = {}  # 低 → 高，允许重复
        # 每个文件自己写入的原始值（不管是否生效）。unload 时靠它把被撤掉的
        # 生效值回退到「剩下文件里优先级最高的那个值」，而不是直接消失。
        self._file_raw: dict[Path, dict[Node, Any]] = {}
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

            if converter is None:
                # 无类型声明 → 原样返回
                value = raw
            elif isinstance(raw, str):
                value = _convert(raw, converter, self._custom_converters)
            else:
                # 非字符串原始值（JSON / TOML 后端的原生结构）：
                # 泛型容器要逐项转（list[Path] 里的字符串 → Path），
                # 标量已经是 typed，不动。
                value = _coerce_native(raw, converter, self._custom_converters)

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

    def remove(self, section: str, key: str, *, persist: bool = True) -> None:
        """删除运行期值（``set`` 的对称操作）。

        只弹出 ``_raw`` 里的值并失效缓存；**故意不动** ``_origins`` /
        ``_definitions`` / ``_by_file``——``flush`` 要靠 origin 找到
        「用户改过的那个文件」，才能把它里面那一行删掉。

        Args:
            persist: True（默认）时把节点标脏，等 ``flush`` 落盘删除；
                False 时只从内存消失，不参与回写。
        """
        node = (section, key)
        with self._lock:
            self._raw.pop(node, None)
            if persist:
                self._dirty.add(node)
            self._invalidate(node)

    def inject_raw(
            self,
            section: str,
            key: str,
            raw: Any,
            *,
            origin: Optional[Path] = None,
            overwrite: bool = False,
    ) -> None:
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
                # 记录「这个文件里这个 key 是什么」——注意与是否生效无关，
                # unload 回退时要靠它。
                self._file_raw.setdefault(origin, {})[node] = raw
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

    # ---------- origin / dirty ----------
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

    def sections_of_path(self, path: Path) -> list[str]:
        """返回该文件**贡献过生效值**的 section 名（去重、排序）。

        「贡献过」= 该节点的生效 origin 是这个文件。被更高优先级文件覆盖掉的
        段不会出现在这里——``tree.<file>.keys()`` 想知道的是「这个文件当前
        提供什么」，而不是「这个文件里写过什么」（后者用
        ``values_of_file`` 看节点级信息）。
        """
        with self._lock:
            nodes = self._by_file.get(Path(path), ())
            return sorted({section for section, _ in nodes})

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

    def values_of_file(self, path: Path) -> dict[Node, Any]:
        """返回该文件写入过的原始值（副本），不论是否生效。"""
        with self._lock:
            return dict(self._file_raw.get(Path(path), {}))

    def forget_file(self, path: Path) -> frozenset[Node]:
        """清除该文件对 Resolver 的全部贡献（``unload()`` 调用）。

        规则：

        1. ``_definitions[node]`` 里移除 ``path``：列表空了就整条 key 删除，
           否则保留其它文件的定义记录（被高优先级文件覆盖的节点，值仍归别人）。
        2. 生效值来自该文件的节点（``_by_file[path]``）**回退**到剩余定义里
           优先级最高、且确实带值的那个文件（逆序遍历 ``_definitions``，
           配合 ``_file_raw`` 取回那个文件自己写的值）；一个都没有才删除节点。
        3. 回退/删除都会递归失效 ``${...}`` 展开缓存。

        返回「生效值来自该文件」的节点集合（调用方据此清 dirty）。

        注意：不带 origin 注入的默认值不属于任何文件，不会被本方法删除。
        """
        path = Path(path)
        with self._lock:
            effective = set(self._by_file.pop(path, set()))
            self._file_raw.pop(path, None)

            # 1) 剥掉该文件在 definitions 里的全部痕迹
            for node, defs in tuple(self._definitions.items()):
                if path not in defs:
                    continue
                remaining = [p for p in defs if p != path]
                if remaining:
                    self._definitions[node] = remaining
                else:
                    self._definitions.pop(node, None)

            # 2) 被撤掉生效值的节点：回退到剩下的最高优先级定义
            for node in effective:
                for candidate in reversed(self._definitions.get(node, ())):
                    values = self._file_raw.get(candidate)
                    if values is not None and node in values:
                        self._raw[node] = values[node]
                        self._origins[node] = candidate
                        self._by_file.setdefault(candidate, set()).add(node)
                        self._invalidate(node)
                        break
                else:
                    self._raw.pop(node, None)
                    self._origins.pop(node, None)
                    self._invalidate(node)

            return frozenset(effective)

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
            self._invalidate((section, key))  # 注册类型后旧缓存必须失效

    def register_custom_converters(self, mapping, *, serializers=None):
        """整体替换自定义转换器表（``{类型: str -> 值}``）。

        Args:
            mapping: 读侧转换器
            serializers: 写侧转换器 ``{类型: 值 -> str}``；不给则写回只能
                ``str(value)``
        """
        with self._lock:
            self._custom_converters = dict(mapping)
            if serializers is not None:
                self._custom_serializers = dict(serializers)

    def register_converter(
            self,
            type_: Any,
            converter: Callable[[str], Any],
            *,
            writer: Optional[Callable[[Any], str]] = None,
    ) -> None:
        """增量注册一个自定义转换器（与 ``configparser`` 的 ``converters`` 同义）。

        只有「构造函数语义不对」的类型才需要注册——普通 ``Enum`` / ``Path``
        子类 / 带 ``X(str)`` 构造的类会被 ``_convert`` 默认回退直接构造。

        **写回同样重要**：不注册 ``writer`` 时 ``flush`` 只能 ``str(value)``。

        注册后已缓存的节点会失效重转，因此可以随时调用。
        """
        with self._lock:
            self._custom_converters[type_] = converter
            if writer is not None:
                self._custom_serializers[type_] = writer
            # 已按旧规则类型化过的节点要重转，注册即时生效
            affected = [
                node for node, conv in self._types.items() if conv is type_
            ]
            for node in affected:
                self._invalidate(node)

    def custom_converters(self) -> dict[Any, Callable[[str], Any]]:
        """返回已注册的自定义转换器（读侧，副本）。"""
        with self._lock:
            return dict(self._custom_converters)

    def custom_serializers(self) -> dict[Any, Callable[[Any], str]]:
        """返回已注册的自定义写回函数（写侧，副本）。``flush`` 用它反解。"""
        with self._lock:
            return dict(self._custom_serializers)

    # ---------- 查询 ----------
    def has_section(self, section: str) -> bool:
        with self._lock:
            return any(s == section for s, _ in self._raw)

    # ---------- 自检 ----------
    def check_all(self) -> None:
        with self._lock:
            for node in tuple(self._raw):
                self.get_typed(*node)

    def typed_nodes(self) -> frozenset[Node]:
        """返回「有类型声明」的全部节点（``@configclass`` 或加载钩子登记过）。

        供审计开关使用（``Configue.load(strict_undeclared=True)``）：文件里
        出现、但既不被 ``@configclass`` 声明、也没有任何 hook 补过类型的
        key，就是「幽灵配置」——它照常生效，但没有任何代码认识它。
        """
        with self._lock:
            return frozenset(self._types)

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
