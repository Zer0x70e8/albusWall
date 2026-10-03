#
"""可观察 / 可持久化的运行时状态容器。

分层设计：
  - ObservableContext    ：观察者 + 脏标记 + 锁 + 子树 root，无持久化。
  - StateContext         ：在 Observable 上下文上叠加 path/autosave。
  - ObservableNamespace   ：只绑定 ObservableContext，可用于不可序列化的
                            运行时状态（socket、锁、线程句柄……）。
  - StatefulNamespace     ：在 Observable 之上叠加 JSON 落盘（save/load）。
  - StateField            ：声明式字段（类型校验 + 默认值），两个基类通用。
"""

import json
import threading
from pathlib import Path
from typing import (
    Any, Callable, Generic, Iterator, Optional, TypeVar,
)

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
# 声明式字段（两个 Namespace 通用）
# ─────────────────────────────────────────────────────────────
class StateField(Generic[T]):
    """声明式 state 字段。用法和普通类属性一致：

        class UserState(ObservableNamespace):
            user_id = StateField(str, default="anon")
            theme   = StateField(str, default="dark")

        s = UserState(user_id="alice")
        s.user_id = "bob"        # 类型校验 + 脏标记，使用方无感
        del s.user_id            # 恢复默认值（__dict__ 里清掉）
    """

    __slots__ = ("type", "default", "name", "nested")

    def __init__(self, type_: Optional[type] = None, *,
                 default: Any = _Missing,
                 nested: Optional[bool] = None) -> None:
        self.type = type_
        self.default = default
        self.name = None
        # type_ 是 Namespace 子类 → 默认嵌套；否则默认不嵌套。
        # 想覆盖可以显式传 nested=True/False。
        if nested is None:
            nested = (
                    isinstance(type_, type)
                    and issubclass(type_, Namespace)
            )
        self.nested = nested

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

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
        if self.type is not None and value is not None \
                and not isinstance(value, self.type):
            raise TypeError(
                f"{type(instance).__name__}.{self.name}: "
                f"expected {self.type.__name__}, got {type(value).__name__}"
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
                value = field.type(_ctx=ctx, **value)

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
