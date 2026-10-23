#
"""Configuration Unified Engine

包含三块：

- ``Resolver`` 的值视图：``StaticConfig``（只读）与 ``Configue``
- 运行期回写：``Configue.set`` / ``flush`` / ``configure_persistence``

回写（flush）语义见 ``Configue.flush`` 的 docstring：
按 origin 把脏节点分组，逐文件用 ``ini_transform.transform`` + ``atomic_write``
写回；单进程约定，不做多进程写保护，也不做外部修改检测。
"""

# import logging
import atexit
import os
import threading
import weakref
from itertools import chain
from pathlib import Path
from typing import (
    Iterable, Literal, Optional, cast, Any, Callable, Iterator,
)

import albuswall
from albuswall.log import getLogger

# import albuswall
from .exceptions import ConfigError, ConfigueErrorSource
from .resolver import Resolver
from .utils.ini_parser import _deconvert
from .utils.ini_transform import transform
from .utils.namespace import Namespace

_logger = getLogger(f"{albuswall.__name__}.configue")

SaveMode = Literal["auto", "defer", "now"]
AutosavePolicy = Literal["off", "manual", "atexit", "debounce"]


class _Configue:
    __slots__ = (
        "static", "dynamic", "resolver",
        "_persistence", "_debounce_interval", "_debounce_timer",
        "_default_write_file", "_last_flush_error", "_flush_lock",
    )

    # logger = logging.getLogger(f"{albuswall.__name__}.configue")

    def __init__(self):
        self.resolver = Resolver()
        self.static = StaticConfig(self.resolver)
        self.dynamic = DynamicConfig()

        # ── 回写状态 ────────────────────────────
        self._persistence: AutosavePolicy = "atexit"
        self._debounce_interval: float = 0.5
        self._debounce_timer: Optional[threading.Timer] = None
        self._default_write_file: Optional[Path] = None
        self._last_flush_error: Optional[ConfigError] = None
        self._flush_lock = threading.RLock()

    def __str__(self):
        return "\n".join((
            f"{type(self).__name__}(",
            f"\t{type(self.static).__name__}: (",
            *chain.from_iterable(
                [f"\t\t{k}: {v}"] if not isinstance(v, Namespace)
                else [f"\t\t{k}:"] + [f"\t\t\t{k_}: {v_}" for k_, v_ in v.items()]
                for k, v in self.static.items()
            ),
            "\t\t),",
            f"\t{type(self.dynamic).__name__}: (",
            *chain.from_iterable(
                [f"\t\t{k}: {v}"] if not isinstance(v, Namespace)
                else [f"\t\t{k}:"] + [f"\t\t\t{k_}: {v_}" for k_, v_ in v.items()]
                for k, v in self.dynamic.items()
            ),
            "\t\t)",
            ")"
        ))


class Configue(_Configue):
    _instance: Optional[_Configue] = None
    _is_initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)  # type: ignore[arg-type]
        return cast(Configue, cls._instance)

    def __init__(self):
        if Configue._is_initialized:
            return
        Configue._is_initialized = True
        super().__init__()
        self.dynamic._configue_ref = weakref.ref(self)
        atexit.register(self._atexit_flush)

    # ══════════════════════════════════════════════════
    # 运行期写 + 回写
    # ══════════════════════════════════════════════════
    def set(
            self,
            section: str,
            key: str,
            value: Any,
            *,
            target: Optional[Path] = None,
            save: SaveMode = "auto",
    ) -> None:
        """写入运行期值，并按持久化策略决定是否立即落盘。

        Args:
            section: 节名
            key: 键名
            value: 已类型化的值（回写时用 ``_deconvert`` 反解）
            target: 显式指定写回文件；不给则按 origin 规则推导
            save: 持久化时机
                - ``"auto"``（默认）：按 ``configure_persistence`` 的策略走
                - ``"defer"``：只标脏，等显式 ``flush()`` / autosave
                - ``"now"``：立即 ``flush(only=[(section, key)])``

        失败语义：本方法不把写盘异常抛给业务——写盘失败只记
        ``last_flush_error`` 并保留 dirty；``save="now"`` 时同样吞掉
        写盘异常（需要严格错误请直接调 ``flush(strict=True)``）。
        参数非法（未知的 save mode）抛 ``ValueError``。
        """
        node = (section, key)
        if save not in ("auto", "defer", "now"):
            raise ValueError(f"unknown save mode: {save!r}")

        self.resolver.set(section, key, value, persist=True)
        if target is not None:
            self.resolver.set_origin(section, key, Path(target))

        if save == "now":
            self.flush(only=[node])
        elif save == "auto":
            self._schedule_persistence()

    def _schedule_persistence(self) -> None:
        """按当前 autosave 策略决定「标脏之后」要不要主动写盘。"""
        if self._persistence == "debounce":
            self._schedule_debounce()
        # off / manual / atexit：只标脏；atexit 在退出时统一 flush

    def _schedule_debounce(self) -> None:
        with self._flush_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            timer = threading.Timer(self._debounce_interval, self._debounce_fire)
            timer.daemon = True
            self._debounce_timer = timer
            timer.start()

    def _debounce_fire(self) -> None:
        with self._flush_lock:
            self._debounce_timer = None
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001  后台线程不能炸
            _logger.error("debounce flush failed: %s", exc)

    def _cancel_debounce(self) -> None:
        with self._flush_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def _atexit_flush(self) -> None:
        """解释器退出时的兜底 flush（atexit / debounce 策略下生效）。"""
        if self._persistence not in ("atexit", "debounce"):
            return
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001  退出路径不能抛
            _logger.error("atexit flush failed: %s", exc)

    @property
    def last_flush_error(self) -> Optional[ConfigError]:
        """最近一次 flush 中第一个写盘错误；成功后不清空，便于排查。"""
        return self._last_flush_error

    def configure_persistence(
            self,
            *,
            autosave: AutosavePolicy = "atexit",
            debounce: float = 0.5,
            default_write_file: Optional[Path] = None,
    ) -> None:
        """设置回写策略。

        Args:
            autosave: 自动写盘策略
                - ``"off"`` / ``"manual"``：set 只标脏，全靠显式 ``flush()``
                - ``"atexit"``（默认）：退出时统一 flush
                - ``"debounce"``：set 后重置一个 ``debounce`` 秒的定时器，
                  到期统一 flush（高频字段必用）
            debounce: debounce 秒数；非负
            default_write_file: 无 origin（运行期新 key）或 origin 只读时的兜底
                写入文件；``None`` 表示不改变当前值

        失败语义：非法 autosave / 负 debounce 抛 ``ValueError``。
        """
        if autosave not in ("off", "manual", "atexit", "debounce"):
            raise ValueError(f"unknown autosave policy: {autosave!r}")
        debounce = float(debounce)
        if debounce < 0:
            raise ValueError(f"debounce must be >= 0, got {debounce}")

        with self._flush_lock:
            self._persistence = autosave
            self._debounce_interval = debounce
            if default_write_file is not None:
                self._default_write_file = Path(default_write_file)
            if autosave != "debounce":
                self._cancel_debounce()

    def flush(
            self,
            *,
            only: Optional[Iterable[tuple[str, str]]] = None,
            strict: bool = False,
    ) -> list[Path]:
        """把脏节点（或指定集合）按 origin 分组，逐文件原子写回。

        分组规则（每个节点独立判定）：
          1. origin 存在且可写 → 写回 origin
          2. origin 存在但只读 → 回退到「也定义过该 key」的最高优先级可写文件；
             没有备选则走 3
          3. origin 不存在（运行期新 key）或只读无备选 → 写 default_write_file

        Args:
            only: 只回写这些节点；``None``（默认）回写 ``resolver.dirty_nodes()``
            strict: True 时遇到写盘错误立即抛 ``ConfigError(source=FILE)``；
                False（默认）时吞掉异常、保留 dirty、记 ``last_flush_error``，
                其余文件继续写

        Returns:
            实际写入成功的文件列表（按写入顺序）。

        失败语义：读文件 / 序列化 / 写盘异常都包装成 ``ConfigError``；
        失败节点保持 dirty，下次 flush 会重试。整个读-改-写过程持有
        ``resolver.lock``，``set`` 不会插进中间。

        参数错误语义：``only`` 里出现 Resolver 中不存在的 node 时直接抛
        ``ConfigError(source=FILE)``（不是静默跳过），避免拼错 key 时
        「以为写了其实没写」。``only=None``（dirty 集合）不做这项校验。
        """
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
                missing = [node for node in nodes if node not in resolver]
                if missing:
                    raise ConfigError(
                        f"flush: unknown nodes {missing}",
                        source=ConfigueErrorSource.FILE,
                        key=",".join(f"[{s}]{k}" for s, k in missing),
                    )

            plans: dict[Path, dict[str, dict[str, str]]] = {}
            plan_nodes: dict[Path, list[tuple[str, str]]] = {}

            for node in nodes:
                section, key = node
                try:
                    target = self._target_file_for(node)
                    if target is None:
                        raise ConfigError(
                            "no writable target file for node "
                            "(set configure_persistence(default_write_file=...))",
                            source=ConfigueErrorSource.FILE,
                            key=f"[{section}]{key}",
                            value=resolver.get_str(section, key),
                        )
                    value = resolver.get_typed(section, key)
                    text = _deconvert(value, resolver.converter_of(section, key))
                except Exception as exc:
                    error = exc if isinstance(exc, ConfigError) else ConfigError(
                        "flush: failed to serialize node",
                        source=ConfigueErrorSource.FILE,
                        key=f"[{section}]{key}",
                        cause=exc,
                    )
                    self._last_flush_error = error
                    _logger.error("flush failed: %s", error)
                    if strict:
                        raise error from exc
                    continue

                plans.setdefault(target, {}).setdefault(section, {})[key] = text
                plan_nodes.setdefault(target, []).append(node)

            for file, edits in plans.items():
                try:
                    self._write_file(file, edits)
                except Exception as exc:  # noqa: BLE001  统一包装
                    error = ConfigError(
                        f"flush: failed to write {file}",
                        source=ConfigueErrorSource.FILE,
                        key=",".join(f"[{s}]{k}" for s, k in plan_nodes[file]),
                        cause=exc,
                    )
                    self._last_flush_error = error
                    _logger.error("flush failed: %s", error)
                    if strict:
                        raise error from exc
                    continue

                resolver.mark_clean(plan_nodes[file])
                written.append(file)
                _logger.info(
                    "flush: file=%s nodes=%d", file, len(plan_nodes[file])
                )

        return written

    # ── 回写内部 ───────────────────────────────────────
    def target_file_of(self, section: str, key: str) -> Optional[Path]:
        """公开视图：节点最终会写回哪个文件（``None`` 表示无处可写）。

        与 ``flush`` 使用同一套规则（origin 可写 → origin；origin 只读 →
        最高优先级可写备选；否则 ``default_write_file``），供设置页 / dump
        在真正写盘前展示归属文件。
        """
        return self._target_file_for((section, key))

    def is_readonly_source(self, path: Path) -> bool:
        """公开视图：来源文件是否只读（显式 ``include.ro`` 或文件系统权限）。"""
        return self._is_readonly(Path(path))

    def _target_file_for(self, node: tuple[str, str]) -> Optional[Path]:
        """按 origin 规则推导节点的写回文件（只读 → 备选 → 默认落点）。"""
        resolver = self.resolver
        origin = resolver.origin_of(*node)
        if origin is not None:
            if not self._is_readonly(origin):
                return origin
            alternative = self._writable_alternative(node, exclude=origin)
            if alternative is not None:
                return alternative
        return self._default_write_file

    def _writable_alternative(
            self, node: tuple[str, str], *, exclude: Path,
    ) -> Optional[Path]:
        """在「也定义过该 node」的文件里挑优先级最高的可写文件。"""
        for path in reversed(self.resolver.definitions_of(*node)):
            if path == exclude:
                continue
            if not self._is_readonly(path):
                return path
        return None

    def _is_readonly(self, path: Path) -> bool:
        """只读判定：显式标记（include.ro）优先，其次看文件系统写权限。

        注意：``os.access(path, os.W_OK)`` 在 root 下恒为真，文件系统权限
        判定会整体失效。以 root 运行时必须用 ``include.ro`` 显式标记只读来源。
        """
        if self.resolver.is_readonly_file(path):
            return True
        return path.exists() and not os.access(path, os.W_OK)

    def _write_file(self, file: Path, edits: dict[str, dict[str, str]]) -> None:
        """读-改-写单个文件：保留注释 / 格式 / 换行符，原子落盘。"""
        # 延迟 import：infrastructure 包的 __init__ 反向依赖 albuswall.configue
        from albuswall.infrastructure.fs import atomic_write

        # 新文件的父目录可能不存在（default_write_file 指向新目录）→ 先建
        file.parent.mkdir(parents=True, exist_ok=True)

        # newline="" 关闭换行符翻译，才能原样探测并保留 \r\n / \n
        if file.exists():
            with file.open("r", encoding="utf-8", newline="") as f:
                original = f.read()
        else:
            original = ""
        eol = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            # transform 追加新 key 时假定每行自带换行，这里补齐最后一行
            lines[-1] += eol
        out = "".join(transform(lines, edits, eol=eol))

        def _write(path: Path) -> None:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write(out)

        atomic_write(file, _write)


# class StaticConfig(FrozenNamespace):
#     """Immutable configuration carrier loaded from a nested dictionary.
#
#     Inherits from FrozenNamespace to provide a read-once, read-many container.
#     """
#
#     def __init__(self, **entries):
#         # Initialize as unfrozen so that load() can write attributes.
#         super().__init__(**entries)
#
#     def load(self, config_dict: dict):
#         """Load configuration from a doubly-nested dictionary.
#
#         Keys become attributes; sub-dictionaries become nested FrozenNamespace
#         instances. Once loaded, the instance is frozen and further attempts to
#         modify it (including calling load again) will raise an AttributeError.
#         """
#         # FrozenNamespace raises AttributeError if already frozen,
#         # so trying to set an attribute here acts as the "already loaded" guard.
#         for key, value in config_dict.items():
#             # Attribute setting will convert any dict value to a nested
#             # FrozenNamespace automatically (via Namespace.__setattr__).
#             setattr(self, key, value)
#
#         # Seal the namespace – no more changes allowed.
#         self.frozen()
#
#     def __str__(self):
#         return f"{type(self).__name__}\n" + ("\t\n".join(
#             f"{k}: {v}" for k, v in self.items()
#         ))

_Missing = object()


class _SectionView:
    __slots__ = ("_resolver", "_section")

    def __init__(self, resolver, section):
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_section", section)

    def __getattr__(self, key):
        return self._resolver.get_typed(self._section, key)

    def __getitem__(self, key):
        return self._resolver.get_typed(self._section, key)

    def ensure(self, key, expected_type=None, default=_Missing):
        resolver = object.__getattribute__(self, "_resolver")
        section = object.__getattribute__(self, "_section")
        node = (section, key)

        # 先决定要不要注入默认值
        if node not in resolver:
            if default is _Missing:
                raise KeyError(f"[{section}]{key}")
            resolver.inject_raw(section, key, default)

        value = resolver.get_typed(section, key)

        if expected_type is not None and value is not None \
                and not isinstance(value, expected_type):
            raise TypeError(
                f"[{section}]{key}: expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return value

    def get(self, key, default=None):
        try:
            return self._resolver.get_typed(self._section, key)
        except KeyError:
            return default

    def __contains__(self, key):
        return (self._section, key) in self._resolver

    def keys(self):
        return self._resolver.keys_of(self._section)

    def items(self):
        for k in self.keys():
            yield k, self._resolver.get_typed(self._section, k)

    def __setattr__(self, k, v):
        raise AttributeError("view is read-only; use _config.resolver.set(...)")

    def __str__(self):
        return f"[{self._section}]"


class StaticConfig(Namespace):
    # 忘记了这个slots到底有没有用了，目前注释掉也没bug出现
    # __slots__ = ("_resolver", "_views")  # 注意：Namespace 用 __dict__，加 slots 要小心

    def __init__(self, resolver):
        super().__init__()
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_views", {})

    def __getattr__(self, name):
        # 只有 __dict__ 里没有时才会走到这里
        resolver = object.__getattribute__(self, "_resolver")
        if resolver.has_section(name):
            views = object.__getattribute__(self, "_views")
            view = views.get(name)
            if view is None:
                view = _SectionView(resolver, name)
                views[name] = view
            return view
        # 顶级 key 懒加载
        try:
            return resolver.get_typed("__default__", name)
        except KeyError as e:
            raise AttributeError(name) from e

    # noinspection method-overriding
    def __setattr__(self, name, value):
        raise AttributeError("StaticConfig is read-only; use _config.resolver.set(...)")

    def items(self):
        resolver = object.__getattribute__(self, "_resolver")
        for sec in resolver.sections():
            if sec == "__default__":
                continue
            yield sec, self.__getattr__(sec)
        for key in resolver.keys_of("__default__"):
            yield key, resolver.get_typed("__default__", key)


# class StaticConfig:
#     __slots__ = ("_resolver", "_views")
#
#     def __init__(self, resolver):
#         object.__setattr__(self, "_resolver", resolver)
#         object.__setattr__(self, "_views", {})
#
#     def __getattr__(self, name):
#         resolver = object.__getattribute__(self, "_resolver")
#         if resolver.has_section(name):
#             views = object.__getattribute__(self, "_views")
#             view = views.get(name)
#             if view is None:
#                 view = _SectionView(resolver, name)
#                 views[name] = view
#             return view
#         # 顶级 key（__default__）
#         return resolver.get_typed("__default__", name)
#
#     def __setattr__(self, name, value):
#         raise AttributeError("StaticConfig is read-only; use _config.resolver.set(...)")
#
#     def items(self):
#         resolver = self._resolver
#         for sec in resolver.sections():
#             yield sec, self.__getattr__(sec)
#         for key in resolver.keys_of("__default__"):
#             yield key, resolver.get_typed("__default__", key)

class DynamicConfig(Namespace):
    """动态配置根——State 子树的挂载点。

    本身不参与状态管理，只提供：
      - mount / mount_lazy / unmount 挂载 API
      - 从根路径访问所有子树（业务层唯一入口）
      - 内部字段过滤（_lazy 等不出现在视图里）

    懒加载两条路径：
      1. `mount_lazy(name, factory)` 后，第一次 `_config.dynamic.<name>`
         才真正调用 factory；
      2. 未 mount 但已通过 `@dynamic_state` 登记的挂载名，`__getattr__`
         直接 fallback 到 ``DYNAMIC_REGISTRY``（因此 setup_dynamic_config
         只需要 import 模块即可；模块由 ``register_dynamic_module`` 在导入期
         登记，配置期统一 import）。

    登记本身是导入期安全的：``@dynamic_state`` 只写登记表，``base_key`` /
    ``filename`` 到这里的首次访问才解析成真实路径。

    从没访问过的子树既不会读盘，也不会在 to_dict() / items() 里出现。
    """

    _INTERNAL_ATTRS = frozenset({"_lazy", "_registry_lock"})

    def __init__(self, **entries: Any) -> None:
        super().__init__()
        object.__setattr__(self, "_lazy", {})
        # DYNAMIC_REGISTRY 的「查表 → 加载 → 发布」需要互斥
        object.__setattr__(self, "_registry_lock", threading.RLock())
        # self._configue_ref: Optional[weakref.ReferenceType[_Configue]] \
        #     = None  # 由外部注入
        # self._logger = None
        for key, value in entries.items():
            setattr(self, key, value)

    # ── 写路径：只做 dict 提升，不做 state 管理 ─────
    # noinspection method-overriding
    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if isinstance(value, dict):
            # 提升为普通 Namespace，而不是 DynamicConfig——
            # 避免嵌套子树继承 _lazy 语义
            value = Namespace(**value)
        object.__setattr__(self, name, value)

    # ── 挂载 API ───────────────────────────────────
    def mount(self, name: str, node: Namespace) -> Namespace:
        """直接挂载一个已构造好的 Namespace（通常是 StatefulNamespace）。"""
        if not isinstance(node, Namespace):
            raise TypeError(
                f"mount expected Namespace, got {type(node).__name__}"
            )
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            raise ValueError(f"mount: name {name!r} is reserved")
        if name in self.__dict__ or name in self.__dict__["_lazy"]:
            raise KeyError(f"{name!r} already mounted")
        object.__setattr__(self, name, node)
        return node

    def mount_lazy(self, name: str, factory: Callable[[], Namespace]) -> None:
        """挂载懒加载工厂。第一次 `_config.dynamic.<name>` 时调用 factory。"""
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            raise ValueError(f"mount_lazy: name {name!r} is reserved")
        if name in self.__dict__:
            raise KeyError(f"{name!r} already mounted")
        lazy = object.__getattribute__(self, "_lazy")
        if name in lazy:
            raise KeyError(f"{name!r} already mounted (lazy)")
        lazy[name] = factory

    def unmount(self, name: str) -> Optional[Namespace]:
        """卸载已挂载的节点。懒加载未触发的节点直接丢弃工厂，返回 None。"""
        if name in self.__dict__ and not name.startswith("_"):
            return self.__dict__.pop(name)
        lazy = object.__getattribute__(self, "_lazy")
        if name in lazy:
            lazy.pop(name)
            return None
        raise KeyError(name)

    # ── 懒加载 ─────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        # 只有在 __dict__ / 类属性里都找不到时才会走到这里
        lazy = self.__dict__.get("_lazy")
        if lazy and name in lazy:
            # 先取 → 再调用 → 成功后才 pop：工厂抛错时 name 仍留在 _lazy，
            # 下次访问可以重试，而不是退化成 _load_registered / AttributeError
            factory = lazy[name]
            node = factory()
            lazy.pop(name, None)
            self.__dict__[name] = node  # 缓存到 __dict__，后续直接命中
            return node

        # 再 fallback 到 DYNAMIC_REGISTRY（setup_dynamic_config 只 import 模块）
        node = self._load_registered(name)
        if node is not None:
            return node

        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def _load_registered(self, name: str) -> Optional[Namespace]:
        """按 ``DYNAMIC_REGISTRY`` 登记信息加载子树；未登记返回 None。

        并发约定：查表与发布在锁内，工厂（读盘）在锁外调用；
        发布前双检，谁先完成就用谁的实例，保证同名只返回一个对象。
        """
        from .dynamic_declaration import DYNAMIC_REGISTRY

        lock = self.__dict__.get("_registry_lock")
        if lock is None:
            return None

        with lock:
            spec = DYNAMIC_REGISTRY.get(name)
        if spec is None:
            return None

        from .declaration import get_config
        # cls 可以是类 / "pkg.mod:Class" / 工厂：统一在访问期解析
        cls = spec.resolve_cls()
        path = spec.resolve_path(get_config().static)
        node = cls.load(path, autosave=spec.autosave, **spec.overrides)

        with lock:
            existing = self.__dict__.get(name)
            if existing is not None:
                return existing
            self.__dict__[name] = node
        return node

    # ── 视图（屏蔽内部字段）───────────────────────
    def _public_items(self) -> Iterator[tuple[str, Any]]:
        for k, v in self.__dict__.items():
            if k in self._INTERNAL_ATTRS or k.startswith("_"):
                continue
            yield k, v

    def keys(self) -> Iterator[str]:
        return (k for k, _ in self._public_items())

    def values(self) -> Iterator[Any]:
        return (v for _, v in self._public_items())

    def items(self) -> Iterator[tuple[str, Any]]:
        return self._public_items()

    def __iter__(self) -> Iterator[str]:
        return self.keys()

    def __len__(self) -> int:
        return sum(1 for _ in self._public_items())

    def __contains__(self, key: str) -> bool:
        if key in self._INTERNAL_ATTRS or key.startswith("_"):
            return False
        if key in self.__dict__:
            return True
        return key in self.__dict__.get("_lazy", {})

    def to_dict(self) -> dict[str, Any]:
        # 未触发的懒加载节点不序列化——它们本来就是"还没加载"的语义
        out: dict[str, Any] = {}
        for k, v in self._public_items():
            if isinstance(v, Namespace):
                out[k] = v.to_dict()
            else:
                out[k] = v
        return out

    def __repr__(self) -> str:
        loaded = ", ".join(self._public_items().__iter__().__next__()[0] for _ in (0,)) if False else ""
        lazy = list(self.__dict__.get("_lazy", {}))
        return (
            f"{type(self).__name__}("
            f"loaded={[k for k, _ in self._public_items()]}, "
            f"lazy={lazy})"
        )

    def __str__(self) -> str:
        return f"{type(self).__name__}\n" + "\t\n".join(
            f"{k}: {v}" for k, v in self.items()
        )

    # @property
    # def logger(self):
    #     if self._logger is not None:
    #         return self._logger
    #         # 回退到 Configue
    #     # 显式解包弱引用
    #     if self._configue_ref is not None:
    #         parent = self._configue_ref()
    #         if parent is not None:
    #             return parent.logger.getChild("dynamic")
    #     raise RuntimeError("DynamicConfig not bound Configue.")
    #
    # @logger.setter
    # def logger(self, value):
    #     self._logger = value
