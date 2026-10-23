#
"""可观察 / 可持久化的运行时状态容器。

分层设计：
  - ObservableContext    ：观察者 + 脏标记 + 锁 + 子树 root，无持久化。
  - StateContext         ：在 Observable 上下文上叠加 path/autosave。
  - ObservableNamespace   ：只绑定 ObservableContext，可用于不可序列化的
                            运行时状态（socket、锁、线程句柄……）。
  - StatefulNamespace     ：在 Observable 之上叠加 JSON 落盘（save/load）。
  - StateField            ：声明式字段（类型校验 + 默认值），两个基类通用。
                            类型既可以显式传（StateField(str)），
                            也可以从类注解兜底（x: str = StateField()）。
"""

import json
import threading
import types
from pathlib import Path
from typing import (
    Any, Callable, Generic, Iterator, Optional, TypeVar,
    Union, get_args, get_origin,
)

from .utils.annotation import resolve_annotation
from .utils.namespace import Namespace

__all__ = [
    "ObservableNamespace",
    "StatefulNamespace",
    "StateField",
    "ObservableContext",
    "StateContext"
]

_Missing = object()
T = TypeVar("T")


# ─────────────────────────────────────────────────────────────
# 上下文
# ─────────────────────────────────────────────────────────────
class ObservableContext:
    """一棵 ObservableNamespace 子树共享的观察 / 脏标记上下文。"""

    __slots__ = ("dirty", "observers", "lock", "root")

    def __init__(self) -> None:
        self.dirty: set[str] = set()
        self.observers: list[Callable[[Any, str], None]] = []
        self.lock = threading.RLock()
        self.root: Optional["ObservableNamespace"] = None

    def attach(self, node: "ObservableNamespace") -> None:
        if self.root is None:
            self.root = node

    # noinspection broad-exception
    def mark_dirty(self, node: "ObservableNamespace", key: str) -> None:
        with self.lock:
            self.dirty.add(key)
            for cb in list(self.observers):
                # 观察者异常不应打断写路径
                try:
                    cb(node, key)
                except Exception:
                    pass


class StateContext(ObservableContext):
    """带持久化路径 / 自动保存开关的上下文。"""

    __slots__ = ("path", "autosave")

    def __init__(self, path: Optional[Path] = None, autosave: bool = False) -> None:
        super().__init__()
        self.path = Path(path) if path else None
        self.autosave = autosave

    def mark_dirty(self, node: "ObservableNamespace", key: str) -> None:
        super().mark_dirty(node, key)
        if self.autosave and self.path and self.root is not None:
            self.root.save()  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────
# 类型的两个静态辅助：嵌套判定 / 实例校验
# ─────────────────────────────────────────────────────────────
# Optional[X] 既可能来自 typing.Union，也可能是 PEP 604 的 X | None
_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


def _analyze_nested(typ: Any) -> tuple[bool, Optional[type]]:
    """判定类型是否应把 dict 自动提升成 Namespace 子树。

    Returns:
        (nested, nested_cls)：
          - ``typ is None``                     → ``(False, None)``
          - ``Optional[X]`` / 单分支 ``Union``   → 递归 ``X``
          - 多分支 ``Union``                     → ``(False, None)``（不支持自动嵌套）
          - ``Namespace`` 子类                   → ``(True, typ)``
          - 其余（含 ``list[X]`` / ``dict[str, X]``）→ ``(False, None)``

    ``nested_cls`` 是解包后的实际类：注解是 ``Optional[UserPrefs]`` 时
    ``type`` 不能直接实例化，必须用 ``nested_cls`` 调构造。
    """
    if typ is None or typ is Any:
        return False, None

    origin = get_origin(typ)
    if origin in _UNION_ORIGINS:
        non_none = [arg for arg in get_args(typ) if arg is not type(None)]
        if len(non_none) == 1:
            return _analyze_nested(non_none[0])
        return False, None

    if isinstance(typ, type) and issubclass(typ, Namespace):
        return True, typ

    return False, None


def _matches_type(value: Any, typ: Any) -> bool:
    """``isinstance`` 的安全版：支持 ``Optional`` / 泛型容器，无法判断时 fail-open。

    先前的 ``isinstance(value, Optional[str])`` 会抛
    ``TypeError: Subscripted generics cannot be used with class and instance checks``，
    这里统一收口：

    - ``Any`` / ``None`` 类型 → True
    - ``Union`` → 任一分支匹配即 True
    - ``list`` / ``tuple`` / ``set`` / ``frozenset`` → 校验容器本身 + 元素（浅层递归）
    - ``dict`` → 校验容器本身；key 类型为 ``str`` 时一并校验，value 按类型校验
    - 其余 → ``isinstance``；抛 ``TypeError`` 时返回 True（fail-open，不误伤）
    """
    if typ is None or typ is Any:
        return True

    origin = get_origin(typ)
    args = get_args(typ)

    if origin in _UNION_ORIGINS:
        return any(_matches_type(value, arg) for arg in args)

    if origin in (list, tuple, set, frozenset):
        if not isinstance(value, origin):
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is ...:  # tuple[int, ...]
            item_types: tuple = (args[0],)
        elif origin is tuple:  # tuple[int, str]
            item_types = args
        else:  # list[int] / set[int]
            item_types = (args[0],)
        for index, item in enumerate(value):
            item_type = (
                item_types[index] if index < len(item_types) else item_types[0]
            )
            if not _matches_type(item, item_type):
                return False
        return True

    if origin is dict:
        if not isinstance(value, dict):
            return False
        if len(args) != 2:
            return True
        key_type, value_type = args
        if key_type is str:
            return all(
                isinstance(k, str) and _matches_type(v, value_type)
                for k, v in value.items()
            )
        return all(_matches_type(v, value_type) for v in value.values())

    try:
        return isinstance(value, typ)
    except TypeError:
        # 无法用于 isinstance 的类型（TypeVar / 奇特的泛型）→ 不校验
        return True


def _type_name(typ: Any) -> str:
    """给报错文案用的类型名（泛型没有 __name__）。"""
    return getattr(typ, "__name__", None) or repr(typ)


# ─────────────────────────────────────────────────────────────
# 声明式字段（两个 Namespace 通用）
# ─────────────────────────────────────────────────────────────
class StateField(Generic[T]):
    """声明式 state 字段。用法和普通类属性一致：

        class UserState(ObservableNamespace):
            user_id = StateField(str, default="anon")   # 显式类型
            theme: str = StateField(default="dark")     # 注解兜底类型

        s = UserState(user_id="alice")
        s.user_id = "bob"        # 类型校验 + 脏标记，使用方无感
        del s.user_id            # 恢复默认值（__dict__ 里清掉）

    类型来源（优先级从高到低）：

    1. 显式 ``type_`` 参数；
    2. 类注解（``x: str = StateField()``），在 ``__set_name__`` 时解析一次；
    3. 都没有 → ``type`` 为 ``None``，不做校验。

    行为总览：

    ================================  ==================  ===========================
    声明                                type / nested        校验
    ================================  ==================  ===========================
    ``StateField(str, default="x")``   str / (F, None)      ``isinstance(v, str)``
    ``x: str = StateField()``          str / (F, None)      同上
    ``StateField()``（无注解）           None / (F, None)     不校验
    ``x: Optional[str]``               Optional[str] / F    ``None`` 或 ``str``
    ``x: list[int]``                   list[int] / F        元素为 int 的 list
    ``p: UserPrefs``                   UserPrefs / (T, C)   dict 自动提升为 ``UserPrefs``
    ``p: Optional[UserPrefs]``         Optional[..] / TC   同上；``None`` 允许
    ``p: list[UserPrefs]``             list[..] / F         不自动嵌套（写死）
    显式 ``nested=False``              任意 / F             不嵌套
    ================================  ==================  ===========================

    约定：``value is None`` 永远放行（即便类型不是 Optional），
    保持 ``StateField(object)`` 这类既有用法不受影响。
    """

    __slots__ = (
        "type", "default", "name", "nested", "nested_cls", "_nested_explicit",
    )

    def __init__(self, type_: Optional[type] = None, *,
                 default: Any = _Missing,
                 nested: Optional[bool] = None) -> None:
        self.type = type_
        self.default = default
        self.name = None
        # nested 三态：True/False = 用户显式指定（不再推断）；None = 待推断
        self._nested_explicit = nested is not None
        self.nested = bool(nested) if nested is not None else False
        self.nested_cls: Optional[type] = None
        if type_ is not None:
            # 显式类型可以立刻推断；注解路径要等 __set_name__
            self._refresh_nested(infer_switch=not self._nested_explicit)

    def _refresh_nested(self, *, infer_switch: bool) -> None:
        """重算 nested / nested_cls；``infer_switch=False`` 时保留用户的开关。"""
        inferred, nested_cls = _analyze_nested(self.type)
        self.nested_cls = nested_cls
        if infer_switch:
            self.nested = inferred

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

        # 注解兜底类型。owner.__annotations__ 是 O(1) 的字典读取；
        # 绝不在这里调 typing.get_type_hints(owner)（n 个字段 = n 次全类遍历）。
        if self.type is None:
            ann = getattr(owner, "__annotations__", {}).get(name)
            if ann is not None:
                self.type = resolve_annotation(ann, owner=owner, field=name)

        # __set_name__ 每个字段只跑一次，之后运行期不再碰注解
        self._refresh_nested(infer_switch=not self._nested_explicit)

    def __get__(self, instance: Any, owner: Optional[type] = None) -> Any:
        if instance is None:
            return self
        if self.name is None:
            raise RuntimeError("StateField not bound to a class attribute")
        d = instance.__dict__
        if self.name in d:
            return d[self.name]
        if self.default is not _Missing:
            return self.default
        raise AttributeError(
            f"{type(instance).__name__}.{self.name} has no value and no default"
        )

    def __set__(self, instance: Any, value: Any) -> None:
        if self.name is None:
            raise RuntimeError("StateField not bound to a class attribute")
        # dict 是「声明为 Namespace 子类」字段的原始形态：显式 nested=False 时
        # 不做提升，也照原样接收（见 README 的行为表）。
        raw_nested_dict = isinstance(value, dict) and self.nested_cls is not None
        if (self.type is not None and value is not None
                and not raw_nested_dict
                and not _matches_type(value, self.type)):
            raise TypeError(
                f"{type(instance).__name__}.{self.name}: "
                f"expected {_type_name(self.type)}, got {type(value).__name__}"
            )
        instance.__dict__[self.name] = value

    def __delete__(self, instance: Any) -> None:
        if self.name is None:
            return
        instance.__dict__.pop(self.name, None)


# ─────────────────────────────────────────────────────────────
# 可观察 Namespace（无持久化）
# ─────────────────────────────────────────────────────────────
class ObservableNamespace(Namespace):
    """可观察的 Namespace 子树：脏标记 + 观察者 + 快照/回滚。

    不绑定任何持久化。适用于持有不可序列化资源的运行时状态容器。

        class Session(ObservableNamespace):
            user_id = StateField(str, default="anon")
            sock    = StateField(object)      # 不可序列化的东西随便放

        s = Session(user_id="alice")
        s.observe(lambda n, k: print("changed:", k))
        s.user_id = "bob"                     # 观察者 + 脏标记
        s.sock = conn                         # 同样被观察
    """

    _INTERNAL_ATTRS = frozenset({"_ctx", "_initializing"})

    # noinspection PyMissingConstructor
    def __init__(self, *, _ctx: Optional[ObservableContext] = None, **entries):
        ctx = _ctx if _ctx is not None else self._make_context()
        object.__setattr__(self, "_ctx", ctx)
        ctx.attach(self)

        object.__setattr__(self, "_initializing", True)
        try:
            for key, value in entries.items():
                setattr(self, key, value)
        finally:
            object.__setattr__(self, "_initializing", False)

        ctx.dirty.clear()  # 构造完统一清一次

    # 子类可覆盖以换用带持久化语义的上下文
    def _make_context(self) -> ObservableContext:
        return ObservableContext()

    # ── 写路径 ─────────────────────────────────────
    # noinspection method-overriding
    def __setattr__(self, name, value):
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        if isinstance(value, dict):
            # 通过类访问描述符：StateField.__get__(None, owner) 返回自身
            field = getattr(type(self), name, None)
            if isinstance(field, StateField) and field.nested:
                ctx = object.__getattribute__(self, "_ctx")
                # nested_cls 是解包后的实际类：注解为 Optional[UserPrefs] 时
                # field.type 不能直接实例化
                cls = field.nested_cls or field.type
                value = cls(_ctx=ctx, **value)

        object.__setattr__(self, name, value)

        if object.__getattribute__(self, "_initializing"):
            return
        ctx = object.__getattribute__(self, "_ctx")
        ctx.mark_dirty(self, name)

    # noinspection method-overriding
    def __delattr__(self, name: str) -> None:
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            object.__delattr__(self, name)
            return
        object.__delattr__(self, name)
        ctx = object.__getattribute__(self, "_ctx")
        ctx.mark_dirty(self, name)

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
        return key in self.__dict__ and not key.startswith("_")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self._public_items():
            if isinstance(v, Namespace):
                out[k] = v.to_dict()
            else:
                out[k] = v
        return out

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_dict()})"

    def __str__(self) -> str:
        return f"{type(self).__name__}\n" + "\t\n".join(
            f"{k}: {v}" for k, v in self.items()
        )

    # ── 观察者 ────────────────────────────────────
    def observe(
            self, callback: Callable[["ObservableNamespace", str], None]
    ) -> Callable[[], None]:
        ctx = object.__getattribute__(self, "_ctx")
        ctx.observers.append(callback)

        def unsubscribe() -> None:
            try:
                ctx.observers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    # ── 脏标记 ────────────────────────────────────
    @property
    def dirty_keys(self) -> frozenset[str]:
        return frozenset(object.__getattribute__(self, "_ctx").dirty)

    def is_dirty(self, key: Optional[str] = None) -> bool:
        dirty = object.__getattribute__(self, "_ctx").dirty
        return (key in dirty) if key else bool(dirty)

    def commit(self) -> None:
        ctx = object.__getattribute__(self, "_ctx")
        with ctx.lock:
            ctx.dirty.clear()

    # ── 快照 / 回滚 ───────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return self.to_dict()

    def restore(self, snapshot: dict[str, Any]) -> None:
        ctx = object.__getattribute__(self, "_ctx")
        for k in list(self.__dict__):
            if k in self._INTERNAL_ATTRS or k.startswith("_"):
                continue
            del self.__dict__[k]
        for k, v in snapshot.items():
            setattr(self, k, v)
        ctx.mark_dirty(self, "*")


# ─────────────────────────────────────────────────────────────
# 可持久化 Namespace
# ─────────────────────────────────────────────────────────────
class StatefulNamespace(ObservableNamespace):
    """在 ObservableNamespace 上叠加 JSON 落盘。

        class UserState(StatefulNamespace):
            user_id = StateField(str, default="anon")
            theme   = StateField(str, default="dark")

        user = UserState.load(Path("user.json"), autosave=True)
        user.user_id = "alice"          # 类型校验 → 脏标记 → 落盘
        user.observe(lambda n, k: print(k))
    """

    def __init__(self, path=None, *, autosave: bool = False,
                 _ctx: Optional[StateContext] = None, **entries):
        ctx = _ctx if _ctx is not None else StateContext(path=path, autosave=autosave)
        super().__init__(_ctx=ctx, **entries)

    def _make_context(self) -> StateContext:
        return StateContext()

    # ── 持久化 ────────────────────────────────────
    def save(self, path: Optional[Path] = None) -> Path:
        ctx: StateContext = object.__getattribute__(self, "_ctx")
        target = Path(path) if path else ctx.path
        if target is None:
            raise ValueError(
                f"{type(self).__name__}.save: no path set; "
                f"pass path= or construct with path="
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        tmp.replace(target)  # 原子替换
        self.commit()
        return target

    @classmethod
    def load(cls, path, *, autosave: bool = False, **overrides):
        path = Path(path)
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        data.update(overrides)
        return cls(path=path, autosave=autosave, **data)
