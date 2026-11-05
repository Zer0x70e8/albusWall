#
"""``Configue`` 主类：单例、生命周期、加载/卸载、回写、树入口。

职责边界：

- 本模块**只做流程编排**：路径解析 → 读盘 → 注入 Resolver → 注册类型/默认值 →
  类型自检 → 汇总报告。值的中枢是 ``Resolver``，声明的中枢是
  ``registry._CLASSES`` / ``registry._FILES``，本类不重复保存状态。
- ``Configue`` 是**单例**：同一个 ``base_dir`` 反复构造返回同一实例；
  换成另一个 ``base_dir`` 直接 ``RuntimeError``——静默换根会让已经拿到的
  旧实例引用指向两个目录，是 bug 温床。
- ``base_dir`` 在构造时 ``resolve()`` 一次绑定，之后 cwd 变化不再影响任何
  相对路径的解析。

``order`` 与覆盖：``load()`` 按 ``FileInfo.order`` **升序**逐个注入，注入
一律 ``overwrite=True``，因此「order 大的后写、赢」；``_definitions`` 的
顺序自然就是「低 → 高」。

后端分派：读写都走 ``backends.get_backend(FileInfo.backend)``。``Configue``
**不认识任何具体格式**——它只决定「写哪个文件的哪个 key」，序列化与落盘由
后端负责（见 ``backends/__init__.py`` 的四条契约）。
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional

from . import registry
from .backends import get_backend
from .bootstrap import load_file
from .errors import (
    AmbiguousConfigFile,
    ConfigClassNotRegistered,
    ConfigError,
    ConfigFileNotFound,
    LoadError,
    LoadReport,
)
from .registry import _CLASSES, _FIELD_OWNER, _FILES, FileInfo
from .resolver import Node, Resolver
from .tree import _ConfigTree
from .utils.ini_transform import DELETE

__all__ = ["Configue"]

_logger = logging.getLogger(__name__)

#: 退出时仍会写盘的文件策略（``off`` / ``manual`` 只认显式 ``flush()``）。
_ATEXIT_POLICIES: frozenset[str] = frozenset({"atexit", "debounce", "now"})


# noinspection protected-member
def _load_hooks() -> tuple[Any, ...]:
    """当前登记的全部加载钩子（快照）。

    延迟 import：``configue/__init__.py`` 顶层 import 了本模块，本模块只能在
    函数体里反向取 ``_LOAD_HOOKS``（否则循环 import）。
    """
    from . import _LOAD_HOOKS

    return tuple(_LOAD_HOOKS)


class Configue:
    """配置单例。

    用法::

        c = Configue(base_dir=Path.home() / ".config" / "app")
        c.load()
        print(Core().debug)          # 业务代码优先用类视图（@configclass 声明）
        c.tree.main.save()           # 树视图仅用于 save / 枚举 / 动态 key

    """  # noqa: D301

    _instance: ClassVar["Configue | None"] = None

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------
    def __new__(cls, base_dir: Path | str) -> "Configue":
        if cls._instance is None:
            instance = super().__new__(cls)
            cls._instance = instance
            instance._initialized = False
            return instance

        existing = cls._instance
        if Path(base_dir).resolve() != existing.base_dir:
            raise RuntimeError(
                f"Configue already initialized with base_dir="
                f"{existing.base_dir}; cannot re-initialize with {base_dir}"
            )
        return existing

    def __init__(self, base_dir: Path | str) -> None:
        if self._initialized:
            return

        self.base_dir = Path(base_dir).resolve()
        self.resolver = Resolver()
        self._closed = False
        #: 树视图写入的声明校验开关（默认关，见 ``strict_tree_writes``）。
        self._strict_tree_writes = False
        self._last_flush_error: Optional[ConfigError] = None
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._flush_lock = threading.RLock()
        #: 已加载文件的反查表（``resolved_path → file name``）。
        #: ``flush`` 拿到 origin 路径后要靠它找到 ``FileInfo``（→ 后端）。
        self._path_to_file: dict[Path, str] = {}
        #: ``section → 注册文件名``：给「没有 @configclass 声明」的段
        #: （``[shortcuts]``）指定写回落点，见 :meth:`register_section_file`。
        self._section_files: dict[str, str] = {}
        self._initialized = True
        atexit.register(self._atexit_flush)

    # ------------------------------------------------------------------
    # 视图 / 代理
    # ------------------------------------------------------------------
    @property
    def tree(self) -> _ConfigTree:
        """配置树入口。视图无状态，每次新建（构造 O(1)）。"""
        return _ConfigTree(self)

    def reg(self, *args, **kwargs) -> None:
        """``registry.reg`` 的代理，方便 ``c.reg(...)``。"""
        registry.reg(*args, **kwargs)

    def configclass(self, *args, **kwargs):
        """``registry.configclass`` 的代理。"""
        return registry.configclass(*args, **kwargs)

    @property
    def last_flush_error(self) -> Optional[ConfigError]:
        """最近一次写盘失败的错误；成功后不清空，便于排查。"""
        return self._last_flush_error

    # ------------------------------------------------------------------
    # strict_tree_writes（审计开关）
    # ------------------------------------------------------------------
    @property
    def strict_tree_writes(self) -> bool:
        """树视图写入是否要求 key「被声明或 hook 登记过」。默认 ``False``。

        默认关：``tree.main.core.new_key = 1`` 是合法写法（树视图是值总线，
        不要求 ``@configclass`` 声明，见 ``tree.py`` 模块 docstring）。

        打开后 ``_SectionView.__setattr__`` 额外要求该 ``(section, key)``
        满足其一，否则抛 ``AttributeError``：

        - ``@configclass`` 声明过（``_FIELD_OWNER`` 里有它）；
        - 加载钩子补过类型（``resolver.typed_nodes()`` 里有它，典型是
          ``[shortcuts].<action_id>`` 这类运行期才知道的 key）。

        用途：把「拼错 key 静默写进文件」从**静默 bug** 变成**当场异常**。
        ``tree.main.core.debgu = True`` 在打开后会立刻
        ``AttributeError``，而不是悄悄在 ini 里多一行 ``debgu``。

        代价：运行期才出现的「全新 key」写不进去了（必须先让 hook 登记类型
        或加 ``@configclass`` 字段）——所以默认关，只在开发/测试期打开。
        """
        return self._strict_tree_writes

    @strict_tree_writes.setter
    def strict_tree_writes(self, value: bool) -> None:
        self._strict_tree_writes = bool(value)

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------
    def check(self) -> LoadReport:
        """只检查声明与路径，不读盘、不动 Resolver。

        检查项：

        1. ``_CLASSES`` 引用了未 ``reg`` 的 file；
        2. 每个 ``FileInfo`` 的 ``file_path`` 能否解析成唯一确定的文件
           （不读内容，只判 ``exists()``；候选规则由后端提供）。
           ``optional=True`` 的文件**缺文件不算错**（与 ``load()`` 一致），
           但**歧义照常报告**；
        3. 该文件的 ``backend`` 是否已注册（TOML / YAML 未实现时在这里暴露，
           报告为 ``read_failed``）。

        不检查：声明冲突（``configclass`` 在装饰期已经校验并可能抛
        ``ConfigDeclarationConflict``）、字段类型
        转换、无默认字段缺失——这些都要读文件才能判断，属于 ``load()``。

        不抛异常，结果全部落在 ``LoadReport.errors``。
        """
        errors: list[LoadError] = []
        warnings: list[str] = []

        for cls, info in _CLASSES.items():
            if info.file not in _FILES:
                errors.append(LoadError(
                    kind="unregistered_file",
                    file=info.file,
                    message=(
                        f"{cls.__qualname__} references unregistered file "
                        f"{info.file!r}"
                    ),
                ))

        for name, finfo in _FILES.items():
            try:
                self._resolve_file_path(finfo)
            except ConfigFileNotFound as exc:
                # optional 文件缺失不是错误：load() 会用占位路径照常加载。
                if not finfo.optional:
                    errors.append(LoadError(
                        kind="file_not_found", file=name, message=str(exc),
                    ))
            except AmbiguousConfigFile as exc:
                errors.append(LoadError(
                    kind="ambiguous_file", file=name, message=str(exc),
                ))
            except ConfigClassNotRegistered:
                # 编程错误（@configclass 未登记 / 拼错），不是「配置缺失」：
                # 当场炸，绝不降级成 read_failed 报告项。
                raise
            except ConfigError as exc:
                # 后端未注册（TOML / YAML 还没实现）也在这里暴露：
                # 严格说这既不是「路径错」也不是「读盘错」，但 kind 取值有限，
                # read_failed 是唯一合理的兜底。
                # 剩余可捕获的只有 get_backend() 的裸 ConfigError（backend 未
                # 注册），没有更具体的类型可收窄；``exc`` 已进 message。
                errors.append(LoadError(
                    kind="read_failed", file=name, message=str(exc),
                ))

        return LoadReport(errors=tuple(errors), warnings=tuple(warnings))

    # ------------------------------------------------------------------
    # load
    # ------------------------------------------------------------------
    def load(
            self,
            *,
            partial: bool = False,
            strict_undeclared: bool = False,
    ) -> LoadReport:
        """加载所有**尚未加载**的已登记文件。

        顺序（不能随意调换）：前置声明检查 → 按 ``order`` 升序逐文件注入 →
        应用待登记的转换器 → 全量注册类型 + 注入默认值 → **执行加载钩子** →
        未声明 key 审计 → ``check_all`` → 汇总。

        幂等：``loaded=True`` 的文件会被跳过，重复调用不会重复注入。加载钩子
        例外——**每次** ``load()`` 都跑（它可能给运行期才知道的 key 补类型，
        要求「重复执行也安全」）。

        ``optional=True`` 的文件缺失时不算错误：以 ``base_dir / file_path``
        作为占位 ``resolved_path`` 标成已加载（默认值照常注入，写回时创建
        文件）；**歧义（多候选）仍然报错**。

        Args:
            partial: True 时不抛 ``ConfigError``，返回 report 由调用方自查。
            strict_undeclared: True 时把「文件里有、但既没被 ``@configclass``
                声明、也没有加载钩子登记类型」的节点收集成
                ``LoadReport.warnings``（**不影响成败、不阻止 key 生效**）。

                这是**审计开关**而不是强制隔离：``[shortcuts]`` 这类段的 key
                是运行期才知道的，强制隔离会把合法配置判成错误；而「幽灵
                配置」恰恰是最难排查的一类问题（值生效了，却没有任何代码读
                它，或者拼错的 key 悄悄多了一行）。默认 ``False``，零行为
                变化。

        失败语义：

        - ``partial=False``（默认）：有任何 error 就抛 ``ConfigError``，
          **已成功加载的文件保留**（不回滚）；``partial=True`` 时不抛，
          返回 report 由调用方自查。
        - hook 抛异常不会中断加载，收集成 ``LoadError(kind="hook_failed")``
          与其它错误同列。
        - 不回滚是有意的：``load()`` 幂等依赖 ``loaded=True`` 标记，回滚会把
          「部分成功」变成「全部失败」，对热插拔场景不友好。
        """
        errors: list[LoadError] = []
        warnings: list[str] = []
        loaded_files: list[str] = []

        # ── Step 1：声明检查（引用未 reg 文件）──────────────────────
        for cls, info in _CLASSES.items():
            if info.file not in _FILES:
                errors.append(LoadError(
                    kind="unregistered_file",
                    file=info.file,
                    message=(
                        f"{cls.__qualname__} references unregistered file "
                        f"{info.file!r}"
                    ),
                ))

        if errors and not partial:
            raise ConfigError(
                "load failed: "
                + "; ".join(e.message or e.kind for e in errors)
            )

        # ── Step 2：待加载文件按 order 升序 ─────────────────────────
        pending = sorted(
            (f for f in _FILES.values() if not f.loaded),
            key=lambda f: f.order,
        )

        # ── Step 3：逐文件 解析路径 → 读盘 → 注入 ───────────────────
        for finfo in pending:
            try:
                actual_path = self._resolve_file_path(finfo)
            except ConfigFileNotFound as exc:
                if not finfo.optional:
                    errors.append(LoadError(
                        kind="file_not_found", file=finfo.name, message=str(exc),
                    ))
                    continue
                # optional 文件缺失：用「按 base_dir 拼出来的规范路径」占位。
                # 读盘会拿到 {}（后端对不存在的文件返回空），默认值照常注入；
                # 因为 resolved_path 已确定，后续 tree.<file>.save() 能在首次
                # 写回时创建文件。歧义**不**走这条路（见下面的分支）。
                actual_path = self._optional_placeholder_path(finfo)
            except AmbiguousConfigFile as exc:
                # 多候选同时存在 = 用户放错了文件；optional 只豁免「不存在」，
                # 所以这里照常报错。
                errors.append(LoadError(
                    kind="ambiguous_file", file=finfo.name, message=str(exc),
                ))
                continue
            except ConfigClassNotRegistered:
                # 编程错误（类未登记 / 拼错）：不得降级成一条 LoadError，
                # 否则启动会带着"某个 @configclass 其实没登记上"继续跑。
                raise
            except ConfigError as exc:
                # 后端未注册（TOML / YAML）走到这里 —— 这是 get_backend() 抛
                # 的裸 ConfigError，没有更具体的类型可收窄；``exc`` 进 message。
                errors.append(LoadError(
                    kind="read_failed", file=finfo.name, message=str(exc),
                ))
                continue

            # resolved_path 必须在读盘前落进 FileInfo：``load_file`` 只认
            # FileInfo（它不知道格式），读失败的文件不会被标成 loaded。
            candidate = replace(
                finfo, loaded=True, resolved_path=actual_path,
            )
            try:
                raw = load_file(candidate)
            except Exception as exc:  # noqa: BLE001  读盘失败不炸整个 load
                errors.append(LoadError(
                    kind="read_failed",
                    file=finfo.name,
                    message=f"failed to read {actual_path}: {exc}",
                ))
                continue

            for (section, key), value in raw.items():
                # overwrite=True：升序遍历下「order 大的」后注入、赢
                self.resolver.inject_raw(
                    section, key, value, origin=actual_path, overwrite=True,
                )

            _FILES[finfo.name] = candidate
            self._path_to_file[actual_path] = finfo.name
            loaded_files.append(finfo.name)

        # ── Step 4 前置：应用「构造前登记的转换器」──────────────────
        self._apply_pending_converters()

        # ── Step 4：全量注册类型 + 注入默认值 ───────────────────────
        # 必须等「所有文件都注入完」再补默认值，否则文件里写了也会被默认值盖掉。
        for _cls, info in _CLASSES.items():
            for f in info.fields:
                section = f.path[0]
                key = f.path[-1]
                self.resolver.register_type(section, key, f.type)
                if f.has_default and (section, key) not in self.resolver:
                    self.resolver.inject_raw(section, key, f.default)

        # ── Step 4.5：加载钩子（在类型自检之前）────────────────────
        # 放这儿有三个原因：hook 里常用 ``register_type`` 批量补类型；在
        # @configclass 之后可以「覆盖」声明类型；在 check_all 之前，hook 注册的
        # 类型才会被校验到。hook 异常**不中断加载**，汇总成 hook_failed。
        for hook in _load_hooks():
            try:
                hook(self.resolver)
            except Exception as exc:  # noqa: BLE001  一个 hook 炸了不影响其它
                errors.append(LoadError(
                    kind="hook_failed",
                    message=(
                        f"{getattr(hook, '__qualname__', hook)!r}: {exc}"
                    ),
                ))

        # ── Step 4.6：未声明 key 审计（默认关）──────────────────────
        # 必须在 hook 之后：hook 补的类型（``[shortcuts]`` 之类）才算「已登记」，
        # 否则合法配置会被误报成幽灵 key。
        if strict_undeclared:
            warnings.extend(self._undeclared_keys())

        # ── Step 5：类型自检 ────────────────────────────────────────
        try:
            self.resolver.check_all()
        except Exception as exc:  # noqa: BLE001  汇总成 LoadError，不直接外泄
            errors.append(LoadError(
                kind="type_check_failed",
                message=f"type check failed: {exc}",
            ))

        # ── Step 6：汇总 ────────────────────────────────────────────
        report = LoadReport(
            loaded_files=tuple(loaded_files),
            skipped_files=tuple(
                f.name for f in _FILES.values()
                if f.loaded and f.name not in loaded_files
            ),
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
        if errors and not partial:
            raise ConfigError(
                f"load failed: {len(errors)} error(s): "
                + "; ".join(e.message or e.kind for e in errors)
            )
        return report

    # ------------------------------------------------------------------
    # unload
    # ------------------------------------------------------------------
    def unload(self, name: str) -> None:
        """卸载一个已加载文件：撤掉它在 Resolver 里的全部贡献。

        幂等：未加载的文件再卸一次什么也不做（不抛）。未登记的名字抛
        ``KeyError``。

        被撤掉生效值的节点会**回退**到「剩余定义文件里优先级最高的那个值」
        （``Resolver`` 按文件保存了各自的原始值）；一个定义文件都不剩时才从
        Resolver 里消失。不带 origin 的默认值不受影响。

        注意：回退**只作用于内存里的 Resolver，不会回滚磁盘**。
        ``tree.main.x = 2`` 之后 ``tree.main.save()``（或 ``autosave`` 已触发）
        已经把 ``2`` 写进 config.ini；此时 ``unload("main")`` 让内存里的 ``x``
        回退到 machine 的 ``1``，但磁盘上 config.ini 仍是 ``2``——再次
        ``load()`` 读到的还是 ``2``。想让磁盘也回退，得在 ``save()`` **之前**
        ``unload()``，或者自己把旧值写回去。
        """
        finfo = _FILES.get(name)
        if finfo is None:
            raise KeyError(f"no such config file: {name!r}")

        if not finfo.loaded or finfo.resolved_path is None:
            self._cancel_debounce(name)
            return

        resolved = finfo.resolved_path
        removed = self.resolver.forget_file(resolved)
        self.resolver.mark_clean(removed)
        self._path_to_file.pop(resolved, None)
        _FILES[name] = replace(finfo, loaded=False, resolved_path=None)
        self._cancel_debounce(name)
        _logger.debug("unload: file=%s nodes=%d", name, len(removed))

    # ------------------------------------------------------------------
    # 声明审计（strict_undeclared / strict_tree_writes 共用）
    # ------------------------------------------------------------------
    def _is_declared(self, section: str, key: str) -> bool:
        """该节点是否「被认识」：``@configclass`` 声明过，或 hook 补过类型。

        两张表都查是有必要的：``_FIELD_OWNER`` 是**声明期**的（装饰时填充，
        不依赖 ``load()``），``resolver.typed_nodes()`` 是**加载期**的
        （``@configclass`` 的字段类型 + 加载钩子补的类型）。只查前者会误杀
        ``[shortcuts]``，只查后者则在 ``load()`` 之前什么都认不出来。
        """
        if (section, key) in _FIELD_OWNER:
            return True
        return (section, key) in self.resolver.typed_nodes()

    def _undeclared_nodes(self) -> list[Node]:
        """「文件里有、但没人认识」的节点（已排序，纯诊断，不改任何状态）。

        ``[shortcuts]`` 这类由 hook 补类型的段会被 ``typed_nodes()`` 认出来，
        不会误报——这正是要求本检查在 hook **之后**运行的原因。
        """
        declared = set(_FIELD_OWNER)
        typed = self.resolver.typed_nodes()
        return [
            node for node in sorted(self.resolver.nodes())
            if node not in declared and node not in typed
        ]

    def _undeclared_keys(self) -> list[str]:
        """``_undeclared_nodes()`` 的 warning 文案形式（``LoadReport.warnings``）。"""
        return [
            f"undeclared key [{section}]{key} "
            f"(no @configclass, no load hook)"
            for section, key in self._undeclared_nodes()
        ]

    def _apply_pending_converters(self) -> None:
        """应用「``Configue`` 构造前登记的转换器」，然后清空队列。

        队列在 ``configue/__init__.py``（模块级），因为 ``register_converter``
        可能在导入期被调用，早于任何 ``Configue`` 实例。
        """
        from . import _PENDING_CONVERTERS

        if not _PENDING_CONVERTERS:
            return
        for type_, reader, writer in _PENDING_CONVERTERS:
            self.resolver.register_converter(type_, reader, writer=writer)
        _PENDING_CONVERTERS.clear()

    # ------------------------------------------------------------------
    # flush
    # ------------------------------------------------------------------
    def flush(
            self,
            *,
            only: Optional[Iterable[Node]] = None,
            strict: bool = False,
            file: Optional[str] = None,
    ) -> list[Path]:
        """把 dirty 节点（或指定集合）按归属文件分组，逐文件原子写回。

        每个节点独立决定「写哪个文件」：origin 可写 → origin；只读 →
        最高优先级可写备选；都没有 → ``_FIELD_OWNER`` 反查的声明文件。
        具体的**序列化与落盘**交给该文件的后端（``FileInfo.backend`` →
        ``Backends.serialize`` / ``Backends.write``），本方法不碰格式细节。

        Args:
            only: 只回写这些节点；``None``（默认）回写 ``resolver.dirty_nodes()``。
                与 ``file`` 互斥。
            strict: True 时遇到写盘错误立即抛 ``ConfigError``；False（默认）
                时吞掉异常、保留 dirty、记 ``last_flush_error``，其余文件继续写。
            file: 只回写该注册文件名下的 dirty 节点（``_FileView.save()`` 的实现
                别名）。文件必须已 ``load()``。

        Returns:
            实际写入成功的文件列表（按写入顺序）。

        Raises:
            KeyError: ``file`` 未登记。
            RuntimeError: ``file`` 已登记但未 ``load()``。
            ValueError: ``file`` 与 ``only`` 同时传。
        """
        if file is not None:
            finfo = _FILES.get(file)
            if finfo is None:
                raise KeyError(f"no such config file: {file!r}")
            if not finfo.loaded or finfo.resolved_path is None:
                raise RuntimeError(f"file {file!r} is not loaded")
            if only is not None:
                raise ValueError(
                    "flush: only= and file= are mutually exclusive"
                )
            target_path = finfo.resolved_path
            only = [
                node for node in sorted(self.resolver.dirty_nodes())
                if self._node_owned_by(node, target_path)
            ]

        return self._flush_impl(only=only, strict=strict)

    def _flush_impl(
            self,
            *,
            only: Optional[Iterable[Node]] = None,
            strict: bool = False,
    ) -> list[Path]:
        """``flush`` 的实际实现（保留原 ``flush(only=, strict=)`` 语义）。"""
        resolver = self.resolver
        written: list[Path] = []

        with self._flush_lock, resolver.lock:
            nodes = [
                tuple(node) for node in
                (only if only is not None else resolver.dirty_nodes())
            ]
            if not nodes:
                return written

            if only is not None:
                # remove() 后的节点既不在 resolver 里也不在 _raw，但仍合法：
                # 它在 dirty 集合里，说明是「待删除」而不是「拼错了」。
                dirty = resolver.dirty_nodes()
                missing = [
                    node for node in nodes
                    if node not in resolver and node not in dirty
                ]
                if missing:
                    raise ConfigError(f"flush: unknown nodes {missing}")

            plans: dict[Path, dict[str, dict[str, Any]]] = {}
            plan_nodes: dict[Path, list[Node]] = {}
            plan_backend: dict[Path, Any] = {}

            for node in nodes:
                section, key = node
                try:
                    target, finfo = self._target_file_for(node)
                    backend = get_backend(finfo.backend)
                    if node in resolver:
                        value = resolver.get_typed(section, key)
                        # 序列化是**后端**的职责：INI 出 str，JSON 出原生值
                        serialized = backend.serialize(
                            value,
                            resolver.converter_of(section, key),
                            resolver.custom_converters(),
                            resolver.custom_serializers(),
                        )
                    else:
                        # remove() 标脏的节点 → 让后端删掉那个 key
                        serialized = DELETE
                except Exception as exc:  # noqa: BLE001  统一包装
                    error = exc if isinstance(exc, ConfigError) else ConfigError(
                        f"flush: failed to serialize node "
                        f"[{section}]{key}: {exc}"
                    )
                    self._last_flush_error = error
                    _logger.error("flush failed: %s", error)
                    if strict:
                        raise error from exc
                    continue

                plans.setdefault(target, {}).setdefault(
                    section, {},
                )[key] = serialized
                plan_nodes.setdefault(target, []).append(node)
                plan_backend[target] = backend

            for target, edits in plans.items():
                try:
                    plan_backend[target].write(target, edits)
                except Exception as exc:  # noqa: BLE001  统一包装
                    error = ConfigError(f"flush: failed to write {target}: {exc}")
                    self._last_flush_error = error
                    _logger.error("flush failed: %s", error)
                    if strict:
                        raise error from exc
                    continue

                resolver.mark_clean(plan_nodes[target])
                written.append(target)
                _logger.info(
                    "flush: file=%s nodes=%d", target, len(plan_nodes[target]),
                )

        return written

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def _optional_placeholder_path(self, finfo: FileInfo) -> Path:
        """``optional`` 文件缺失时的占位路径：``base_dir / file_path`` 的规范形。

        与 ``_resolve_file_path`` 的区别：**不查文件系统**、不走后端的候选规则
        （后缀补全没有意义——没有任何候选存在）。绝对路径原样使用（pathlib 的
        ``/`` 遇到绝对右值会丢弃左值）。

        占位路径精确为 ``base_dir / file_path``，不应用后端的候选规则
        （``.ini`` / ``.conf.ini`` / ``stem + .ini`` 等）。这是 ``ignore_missing``
        的显式语义：文件不存在时假设「它将出现在声明的位置」。若之后文件以候选
        路径（如 ``config.ini`` 而非 ``config.conf``）出现，``load()`` 会优先读
        候选，此时之前写进占位路径的内容不会被自动合并。
        """
        raw = Path(finfo.file_path).expanduser()
        base = raw if raw.is_absolute() else (self.base_dir / raw)
        return base.resolve()

    def _resolve_file_path(self, finfo: FileInfo) -> Path:
        """把 ``FileInfo.file_path`` 解析成唯一确定的绝对路径。

        真正的候选规则属于**后端**（INI 补 ``.ini``、JSON 补 ``.json``）：
        本方法只负责「相对 ``base_dir`` 展开 + 规范化」，然后交给
        ``get_backend(finfo.backend).resolve()``。0 个命中 →
        ``ConfigFileNotFound``，多个命中 → ``AmbiguousConfigFile``，后端未注册
        → ``ConfigError``（不猜，让调用方改配置或写绝对路径）。

        每次 ``load()`` 都会重新解析（**不在 reg 时缓存**），因此运行期新建
        的文件、``rename`` 之后的行为都是可预期的。
        """
        raw = Path(finfo.file_path).expanduser()
        base = raw if raw.is_absolute() else (self.base_dir / raw)
        backend = get_backend(finfo.backend)
        return Path(backend.resolve(base)).resolve()

    # ------------------------------------------------------------------
    # 归属判定（flush 内部）
    # ------------------------------------------------------------------
    def _node_owned_by(self, node: Node, path: Optional[Path]) -> bool:
        """节点是否归属该文件：生效 origin 是它，或最终写回目标是它。"""
        if path is None:
            return False
        if self.resolver.origin_of(*node) == path:
            return True
        try:
            target, _finfo = self._target_file_for(node)
        except ConfigClassNotRegistered:
            # 编程错误（类未登记 / 拼错）必须当场炸：它不是"这个节点没有归属
            # 文件"（那是正常的 False），而是"某份配置类压根没登记"。
            raise
        except ConfigError as exc:
            # 正常分支：该节点不属于任何已加载文件（运行期新 key / 无 origin）。
            # 这里返回 False 只是"本文件不写它"，可以安全继续 —— 无归属的
            # 节点不会因此被静默丢掉：``flush()``（不带 file=）根本不走本方法，
            # 而是在 ``_flush_impl`` 里对每个 dirty 节点重新调
            # ``_target_file_for`` 并抛 ``ConfigError``。
            _logger.debug(
                "node %s has no owning file (%s); treat as not owned by %s",
                node, exc, path,
            )
            return False
        return target == path

    def _target_file_for(self, node: Node) -> tuple[Path, FileInfo]:
        """推导节点的写回目标：``(写回路径, 所属 FileInfo)``。

        规则的唯一实现在 :meth:`target_file_of`（公开入口，schema / dump 共用）；
        本方法只是再补上「用哪个后端」——「写哪个文件」和「用什么格式写」是
        同一个决定。
        """
        path = self.target_file_of(node[0], node[1])
        return path, self._finfo_for_path(path)

    def target_file_of(self, section: str, key: str) -> Path:
        """该节点写回时会落到哪个文件（绝对路径）。

        与 ``flush`` 完全同一套规则，四步：

        1. 生效值来自某个可写文件 → 就是它（origin）；
        2. origin 只读 → 「也定义过该节点」的文件里优先级最高的可写文件；
        3. 都没有（默认值 / 运行期新 key）→ ``registry.field_owner`` 反查
           声明该字段的文件，取其已解析路径；
        4. 仍然没有 → 抛 ``ConfigError``（这个 key 不属于任何已加载文件）。

        只读判定走 :meth:`is_readonly_path`：显式 ``mark_readonly_file`` 标记
        优先，其次看文件系统写权限（root 下 ``os.access`` 恒真，靠显式标记）。
        """
        resolver = self.resolver
        origin = resolver.origin_of(section, key)
        if origin is not None:
            if not self.is_readonly_path(origin):
                return origin
            alternative = self._writable_alternative(
                (section, key), exclude=origin,
            )
            if alternative is not None:
                return alternative

        owner = _FIELD_OWNER.get((section, key))
        if owner is None:
            # 运行期才知道 key 的段（``[shortcuts]``）：没有 @configclass 字段，
            # 只能靠 ``register_section_file`` 登记的「段级写回落点」。
            owner = self._section_files.get(section)
        if owner is None:
            raise ConfigError(
                f"no owning file for [{'/'.join((section, key))}]; "
                f"not declared by any @configclass"
            )
        finfo = _FILES.get(owner)
        if finfo is None:
            raise ConfigError(
                f"field [{section}]{key} declares file {owner!r} "
                f"which is not registered"
            )
        if not finfo.loaded or finfo.resolved_path is None:
            raise ConfigError(
                f"field [{section}]{key} declares file {owner!r} "
                f"which is not loaded"
            )
        return finfo.resolved_path

    def register_section_file(self, section: str, name: str) -> None:
        """登记「某个段没有 ``@configclass`` 声明时，默认写回哪个文件」。

        给运行期才知道 key 的段用（典型：``[shortcuts]``）。这类节点的 key
        不是声明出来的，``_FIELD_OWNER`` 里没有它们，于是
        :meth:`target_file_of` 在「origin 只读（随包默认表）+ 用户文件里还
        没有这个 key」时推不出写回落点——本方法补的就是这一步。

        规则位置：``target_file_of`` 的**最后一步兜底**（origin 与
        「其它定义过它的可写文件」都推不出来时才用），因此：

        * 已声明字段（``config.ini`` 的段）不受影响；
        * 用户文件里已经写了这个 key 时，生效 origin 就是它，本登记不参与。

        Args:
            section: 段名，如 ``"shortcuts"``。
            name: ``reg()`` 时用的注册名（如 ``"shortcuts"`` = 用户覆盖表）。

        Raises:
            ValueError: ``section`` / ``name`` 为空。
        """
        text = str(section or "").strip()
        target = str(name or "").strip()
        if not text or not target:
            raise ValueError(
                "register_section_file: section and name must be non-empty"
            )
        self._section_files[text] = target

    def is_readonly_path(self, path: Path) -> bool:
        """该路径是否只读（显式标记优先，其次看文件系统写权限）。

        只读的文件 ``flush`` 不会写，``target_file_of`` 会改挑「另一个也定义
        过该节点的可写文件」。``resolver.mark_readonly_file`` 是 root 环境下
        唯一可靠的判定来源。
        """
        return self._is_readonly(path)

    def dirty_nodes(self) -> frozenset[Node]:
        """当前待回写的节点集合（``resolver.dirty_nodes()`` 的薄包装）。"""
        return self.resolver.dirty_nodes()

    # ------------------------------------------------------------------
    # 变更广播
    # ------------------------------------------------------------------
    def subscribe(
            self,
            callback: Any,
            *,
            nodes: Iterable[Node] | Node | None = None,
    ) -> Any:
        """订阅配置变更，返回**幂等**的取消函数。

        ``callback(node, value, source)``，``node`` 是 ``(section, key)``，
        ``source`` 是四种来源之一：

        ==========  ==================================================
        ``set``     运行期赋值（``resolver.set`` / 树视图写入）
        ``remove``  运行期删值（``resolver.remove``）
        ``reset``   恢复默认（``Configue.reset``）
        ``reload``  ``load()`` 重新读到**不同的值**
        ==========  ==================================================

        ``flush()`` **不**触发广播：它只落盘，不改变任何值。

        回调在**锁外**执行，因此回调里再 ``set`` 一个配置不会死锁；单个
        回调抛异常会被 ``logger.exception`` 吃掉，不影响其它订阅者，也不
        冒泡到写配置的调用方（广播是旁路，不能改变写配置的成败）。

        Args:
            callback: ``callback(node, value, source)``。
            nodes: ``None`` 订阅全部；给了节点（``("section", "key")`` 或
                其可迭代）就只收这些节点的事件。

        Returns:
            取消函数（重复调用不抛）。

        典型用法（快捷键注册表订阅自己那个段）::

            unsubscribe = config.subscribe(
                lambda node, value, source: rebind(),
                nodes=[(section, key) for key in config.resolver.keys_of("shortcuts")],
            )

        .. note::
           订阅表在 ``resolver.events``（``EventHub``）里，不在 ``Resolver``
           自己身上——事件分发与「值的唯一中枢」是两件事，混在一起会让
           ``Resolver`` 的临界区变成锁中锁。
        """
        return self.resolver.events.subscribe(callback, nodes=nodes)

    def _finfo_for_path(self, path: Path) -> FileInfo:
        """``resolved_path`` → 已加载的 ``FileInfo``（找不到就是内部状态不一致）。"""
        name = self._path_to_file.get(path)
        if name is None:
            raise ConfigError(f"no registered file owns path {path}")
        finfo = _FILES.get(name)
        if finfo is None or finfo.resolved_path != path:
            raise ConfigError(
                f"stale file mapping for {path} → {name!r}; "
                f"call load() again"
            )
        return finfo

    def _writable_alternative(
            self, node: Node, *, exclude: Path,
    ) -> Optional[Path]:
        """在「也定义过该 node」的文件里挑优先级最高的可写文件。"""
        for path in reversed(self.resolver.definitions_of(*node)):
            if path == exclude:
                continue
            if not self._is_readonly(path):
                return path
        return None

    def _is_readonly(self, path: Path) -> bool:
        """只读判定：显式标记优先，其次看文件系统写权限。

        注意：``os.access(path, os.W_OK)`` 在 root 下恒为真，权限判定会整体
        失效；以 root 运行时要靠 ``resolver.mark_readonly_file`` 显式标记。
        """
        if self.resolver.is_readonly_file(path):
            return True
        return path.exists() and not os.access(path, os.W_OK)

    # ------------------------------------------------------------------
    # 持久化调度（FileInfo 级策略）
    # ------------------------------------------------------------------
    def _schedule_persist(self, name: str) -> None:
        """按该文件的 ``autosave`` 策略决定「标脏之后」要不要主动写盘。

        - ``off`` / ``manual`` / ``atexit``：只标脏（``atexit`` 在退出时统一写）
        - ``debounce``：重置该文件的定时器（键 = file name）
        - ``now``：立即 ``flush(file=name)``

        文件未加载时什么也不做：没解析出路径，写盘必然失败。
        """
        finfo = _FILES.get(name)
        if finfo is None or not finfo.loaded:
            return

        policy = finfo.autosave
        if policy == "now":
            try:
                self.flush(file=name)
            except Exception as exc:  # noqa: BLE001  写盘失败不打断业务赋值
                self._record_flush_error(f"flush(now) failed for {name!r}", exc)
        elif policy == "debounce":
            with self._flush_lock:
                old = self._debounce_timers.pop(name, None)
                if old is not None:
                    old.cancel()
                timer = threading.Timer(
                    max(float(finfo.debounce), 0.0),
                    self._debounce_fire,
                    args=(name,),
                )
                timer.daemon = True
                self._debounce_timers[name] = timer
                timer.start()

    def _debounce_fire(self, name: str) -> None:
        with self._flush_lock:
            self._debounce_timers.pop(name, None)
        try:
            self.flush(file=name)
        except Exception as exc:  # noqa: BLE001  后台线程不能炸
            self._record_flush_error(f"debounce flush failed for {name!r}", exc)

    def _cancel_debounce(self, name: str) -> None:
        with self._flush_lock:
            timer = self._debounce_timers.pop(name, None)
            if timer is not None:
                timer.cancel()

    def _record_flush_error(self, context: str, exc: Exception) -> None:
        error = exc if isinstance(exc, ConfigError) else ConfigError(
            f"{context}: {exc}"
        )
        self._last_flush_error = error
        _logger.error("%s: %s", context, exc)

    def _atexit_flush(self) -> None:
        """解释器退出时的兜底写盘（只写「自动」策略的文件）。"""
        if getattr(self, "_closed", True):
            return
        for name, finfo in list(_FILES.items()):
            if not finfo.loaded or finfo.autosave not in _ATEXIT_POLICIES:
                continue
            try:
                self.flush(file=name)
            except Exception as exc:  # noqa: BLE001  退出路径不能抛
                _logger.error("atexit flush failed for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------
    def close(self) -> None:
        """幂等清理：注销 atexit、取消所有 debounce 定时器、flush 全部 dirty。

        **不重置** ``Configue._instance``：``close()`` 是「显式清理」而不是
        「销毁」，之后仍可以 ``c.load()`` / 再构造同 ``base_dir`` 的实例
        （返回的仍是这个已关闭对象）。测试要彻底重置请直接
        ``Configue._instance = None``。
        """
        if self._closed:
            return
        self._closed = True

        with contextlib.suppress(Exception):  # 未注册过也无妨
            atexit.unregister(self._atexit_flush)

        with self._flush_lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()

        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001  close 不把写盘错误抛给调用方
            _logger.error("close: flush failed: %s", exc)

        # 订阅者（典型是进程级快捷键注册表）不该被一个已关闭的配置实例
        # 长期持有：清掉订阅表，避免「关了又开」时旧回调还在收事件。
        self.resolver.events.clear()

    def __repr__(self) -> str:
        return (
            f"Configue(base_dir={str(self.base_dir)!r}, "
            f"files={sorted(_FILES)}, closed={self._closed})"
        )
