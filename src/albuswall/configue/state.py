#
"""StatefulNamespace 和 StateField：可持久化的运行时状态容器。

设计要点：
  - 值存在 instance.__dict__ 里，和普通属性完全一样；StateField 只
    负责类型校验 + 默认值回退，让使用方感觉不到描述符的存在。
  - 写路径统一由 StatefulNamespace.__setattr__ 走脏标记 / autosave；
    StateField.__set__ 只做值落盘（object.__setattr__ 会自动调它）。
  - 一棵子树共享一个 _StateContext：脏标记、观察者、持久化文件。
"""

import json
import threading
from pathlib import Path
from typing import (
    Any, Callable, Generic, Iterator, Optional, TypeVar,
)

from .utils.namespace import Namespace

__all__ = ["StatefulNamespace", "StateField"]

_Missing = object()
T = TypeVar("T")


class _StateContext:
    """一棵 StatefulNamespace 子树共享的状态上下文。"""

    __slots__ = ("path", "autosave", "dirty", "observers", "lock", "root")

    def __init__(self, path: Optional[Path] = None, autosave: bool = False) -> None:
        self.path = Path(path) if path else None
        self.autosave = autosave
        self.dirty: set[str] = set()
        self.observers: list[Callable[[Any, str], None]] = []
        self.lock = threading.RLock()
        self.root: Optional["StatefulNamespace"] = None

    def attach(self, node: "StatefulNamespace") -> None:
        if self.root is None:
            self.root = node

    def mark_dirty(self, node: "StatefulNamespace", key: str) -> None:
        with self.lock:
            self.dirty.add(key)
            for cb in list(self.observers):
                # noinspection broad-exception
                try:
                    cb(node, key)
                except Exception:
                    pass
            if self.autosave and self.path and self.root is not None:
                self.root.save()


class StateField(Generic[T]):
    """声明式 state 字段。用法和普通类属性一致：

        class UserState(StatefulNamespace):
            user_id = StateField(str, default="anon")
            theme   = StateField(str, default="dark")

        s = UserState(user_id="alice")
        s.user_id = "bob"        # 类型校验 + 脏标记，使用方无感
        del s.user_id            # 恢复默认值（__dict__ 里清掉）

    实现上和 ConfigField 同构——数据描述符——但值不托管给外部
    resolver，而是直接落在 instance.__dict__[name]，让使用方完全
    感觉不到代理。
    """

    __slots__ = ("type", "default", "name")

    def __init__(self, type_: Optional[type] = None, *,
                 default: Any = _Missing) -> None:
        self.type = type_
        self.default = default
        self.name: Optional[str] = None

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


class StatefulNamespace(Namespace):
    """可持久化的 Namespace 子树。

    典型用法：

        class UserState(StatefulNamespace):
            user_id = StateField(str, default="anon")
            theme   = StateField(str, default="dark")

        user = UserState.load(Path("user.json"), autosave=True)
        user.user_id = "alice"          # 类型校验 → 脏标记 → 落盘
        user.observe(lambda n, k: print(k))
    """

    _INTERNAL_ATTRS = frozenset({"_ctx"})

    # noinspection PyMissingConstructor
    def __init__(
            self,
            path: Optional[Path] = None,
            *,
            autosave: bool = False,
            _ctx: Optional[_StateContext] = None,
            **entries: Any,
    ) -> None:
        ctx = _ctx if _ctx is not None else _StateContext(path=path, autosave=autosave)
        object.__setattr__(self, "_ctx", ctx)
        ctx.attach(self)

        for key, value in entries.items():
            setattr(self, key, value)  # 走 __setattr__ → StateField.__set__

        # 构造不算修改
        if _ctx is None:
            ctx.dirty.clear()

    # ── 写路径 ─────────────────────────────────────
    # noinspection method-overriding
    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        # dict → 同类型子树，共享 _ctx
        if isinstance(value, dict):
            ctx = object.__getattribute__(self, "_ctx")
            value = type(self)(_ctx=ctx, **value)

        # object.__setattr__ 会自动调用数据描述符 __set__（StateField）
        # 或者把值直接写进 instance.__dict__
        object.__setattr__(self, name, value)

        # 无论走描述符还是普通属性，这里统一记脏
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
            self, callback: Callable[["StatefulNamespace", str], None]
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

    # ── 持久化 ────────────────────────────────────
    def save(self, path: Optional[Path] = None) -> Path:
        ctx = object.__getattribute__(self, "_ctx")
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
    def load(
            cls,
            path: Path,
            *,
            autosave: bool = False,
            **overrides: Any,
    ) -> "StatefulNamespace":
        path = Path(path)
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        data.update(overrides)
        return cls(path=path, autosave=autosave, **data)

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
