#
"""Application-level replaceable slots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar

from .registry import hooks_map

if TYPE_CHECKING:
    from .container import Container

T = TypeVar("T")
logger = logging.getLogger(__package__)

__all__ = [
    "Slot", "SlotCandidate", "SlotRegistry",
    "SlotError", "SlotConflictError", "SlotFrozenError",
    "SlotHostOnlyError", "SlotNotResolvedError",
]


class SlotError(RuntimeError):
    """槽位操作错误基类。"""


class SlotConflictError(SlotError):
    """最高优先级有 2+ 个非默认候选，无法仲裁。"""


class SlotFrozenError(SlotError):
    """槽位已经 resolve，或已 started，不允许再改。"""


class SlotHostOnlyError(SlotError):
    """插件试图修改 host-only 槽位。"""


class SlotNotResolvedError(SlotError):
    """访问了尚未 resolve 的槽位。"""


@dataclass(frozen=True)
class SlotCandidate(Generic[T]):
    name: str
    factory: Callable[["Container"], T]
    priority: int = 0
    #: ``"core"`` / ``"host"`` / ``"plugin:<name>"``；冲突报错与调试用
    source: str = "host"


class Slot(Generic[T]):
    """一个可替换的基础设施点。

    - 有且只有一个默认候选（``source="core"``，``priority=-1000``）
    - 默认候选**永不参与冲突判定**：允许任意宿主/插件候选与之共存
    - ``host_only=True`` 时，``source`` 以 ``"plugin:"`` 开头的注册被拒绝
    - ``resolve`` 之后冻结：``register`` 抛错，``revoke`` 返回 False
    """

    def __init__(
            self,
            name: str,
            default: SlotCandidate[T],
            *,
            select_hook: str,
            host_only: bool = False,
    ) -> None:
        self.name = name
        self.select_hook = select_hook
        self.host_only = host_only
        self._default = default
        self._candidates: dict[str, SlotCandidate[T]] = {default.name: default}
        self._resolved: T | None = None
        self._started = False

    # ---------------- 状态 ----------------
    @property
    def resolved(self) -> T | None:
        return self._resolved

    @property
    def started(self) -> bool:
        return self._started

    def mark_started(self) -> None:
        """冻结整个生命周期（run 阶段不可替换）。"""
        self._started = True

    # ---------------- 登记 ----------------
    def register(
            self,
            name: str,
            factory: Callable[["Container"], T],
            *,
            priority: int = 0,
            source: str = "host",
    ) -> None:
        if self._resolved is not None:
            raise SlotFrozenError(
                f"slot {self.name!r} is already resolved")
        if self.host_only and source.startswith("plugin:"):
            raise SlotHostOnlyError(
                f"slot {self.name!r} can only be provided by core or host, "
                f"got source={source!r}")
        self._candidates[name] = SlotCandidate(name, factory, priority, source)

    def set_explicit(self, value: T) -> None:
        """显式注入：跳过仲裁，直接固化。"""
        if self._started:
            raise SlotFrozenError(
                f"slot {self.name!r} is already started")
        self._resolved = value

    def revoke(self, name: str) -> bool:
        """撤销候选（插件回滚用）。默认候选与已 resolve 的槽位不可撤销。"""
        if self._resolved is not None or name == self._default.name:
            return False
        return self._candidates.pop(name, None) is not None

    # ---------------- 仲裁 ----------------
    def resolve(self, container: "Container") -> T:
        if self._resolved is not None:
            return self._resolved

        ordered = sorted(
            self._candidates.values(),
            key=lambda c: c.priority,
            reverse=True,
        )
        top = ordered[0].priority
        winners = [c for c in ordered if c.priority == top]
        non_default = [c for c in winners if c.source != "core"]
        if len(non_default) > 1:
            names = ", ".join(f"{c.name}({c.source})" for c in non_default)
            raise SlotConflictError(
                f"slot {self.name!r}: {len(non_default)} candidates at "
                f"priority {top}: {names}; use set_{self.name}() or "
                f"assign explicit priorities")
        winner = non_default[0] if non_default else winners[0]
        logger.debug("slot %r resolved to %r (%s)",
                     self.name, winner.name, winner.source)
        self._resolved = winner.factory(container)
        return self._resolved

    def __repr__(self) -> str:
        state = "started" if self._started else (
            "resolved" if self._resolved else "open")
        names = ", ".join(
            f"{c.name}@{c.priority}({c.source})"
            for c in self._candidates.values())
        return f"<Slot {self.name!r} [{names}] {state}>"


class SlotRegistry:
    """固定名单的槽位集合。名单本身不可在运行期扩充。"""

    def __init__(self) -> None:
        self._slots: dict[str, Slot[Any]] = {}

    def define(self, slot: Slot[Any]) -> None:
        if slot.name in self._slots:
            raise SlotError(f"slot {slot.name!r} is already defined")
        if slot.select_hook not in hooks_map:
            raise SlotError(
                f"slot {slot.name!r}: select_hook {slot.select_hook!r} "
                f"is not registered in hooks_map")
        self._slots[slot.name] = slot

    def get(self, name: str) -> Slot[Any]:
        try:
            return self._slots[name]
        except KeyError:
            raise SlotError(f"unknown slot {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._slots

    def names(self) -> list[str]:
        return list(self._slots)

    def __repr__(self) -> str:
        return f"<SlotRegistry {self.names()}>"


def build_default_slots() -> SlotRegistry:
    """构造应用默认槽位。宿主/插件在此之上 register 候选。"""
    from .main_loop import HeadlessMainLoop
    from .plugins import PluginManager

    registry = SlotRegistry()

    # ---- config: host-only，resolve 在 select_config phase ----
    registry.define(Slot(
        "config",
        default=SlotCandidate(
            "configue", factory=lambda c: c.require("config"),
            priority=-1000, source="core",
        ),
        select_hook="select_config",
        host_only=True,
    ))

    # ---- plugins: host-only，resolve 在 select_plugins phase ----
    # 注意 factory 是 ``lambda c: PluginManager(c.require("app"))``，
    # 而不是闭包捕获 self —— 这样 Slot 与 CoreApplication 解耦。
    registry.define(Slot(
        "plugins",
        default=SlotCandidate(
            "default", factory=lambda c: PluginManager(c.require("app")),
            priority=-1000, source="core",
        ),
        select_hook="select_plugins",
        host_only=True,
    ))

    # ---- main_loop: 允许插件提供，resolve 在 select_loop phase ----
    registry.define(Slot(
        "main_loop",
        default=SlotCandidate(
            "headless", factory=lambda _c: HeadlessMainLoop(),
            priority=-1000, source="core",
        ),
        select_hook="select_loop",  # 槽位名与 hook 名解耦
        host_only=False,
    ))

    return registry
