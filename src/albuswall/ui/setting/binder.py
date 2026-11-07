#
"""一个叶子的绑定器（``key → LeafBinder``）。

视图的私有状态只有两处：**binder 表**与**锚点表**。binder 管一个叶子的
「初值快照 / 当前值 / dirty / 错误」，与 Qt 无关，因此可以在没有
``QApplication`` 的环境里单独测。

值的来源只有两条
================

* :meth:`LeafBinder.set_from_ui` —— 控件被用户改动（视图内部，不回环）；
* :meth:`LeafBinder.set_external` —— 外部通知（``set`` / ``remove`` /
  ``reset``），**权威值**，会刷新初值快照并清 dirty/错误。

「dirty」= 用户值 ≠ 初值快照，**或**上一次写回留下的 ``backend_dirty``
（configue dump 的 ``dirty`` 字段）。后者只有 :meth:`mark_persisted` 能清，
因为「值没变但还没落盘」只有写回层知道。

**不在这里做的事**：提交、写盘、判断哪些键该提交。视图只吐
``{key: typed_value}``，写回层自己算 ``changed``。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from .contract import DisabledReason, editability

__all__ = ["BinderEvent", "LeafBinder"]

#: 监听器收到的事件名：
#:
#: * ``"ui"``：用户改动（row 的控件已经是最新值，不需要回写控件）；
#: * ``"external"``：外部通知（row 必须把控件同步到 binder.value）；
#: * ``"reset"``：点了「重置」（row 要同步控件）；
#: * ``"error"``：错误出现 / 清除（row 只改图标与 tooltip）。
BinderEvent = str

_MISSING = object()


def _same(left: Any, right: Any) -> bool:
    """值比较；比较不出来就算「不同」（宁可多标一次脏）。"""
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001  自定义对象比较炸了不能影响 UI
        return False


class LeafBinder:
    """一个叶子的 UI 侧状态。

    Args:
        leaf: 已 :func:`normalize_leaf` 的叶子 dict。
        editable: 覆盖可编辑判定（默认按 ``editability(leaf)`` 算）。
            ``None`` 表示用契约判定；显式传 ``False`` 用于视图额外加锁
            （例如演示里的「只读快照」）。
    """

    def __init__(
            self,
            leaf: Mapping[str, Any],
            *,
            editable: bool | None = None,
    ) -> None:
        self.leaf: dict[str, Any] = dict(leaf)
        self.key: str = str(self.leaf["key"])
        can_edit, reason, detail = editability(self.leaf)
        self.reason: DisabledReason = reason
        self.reason_detail: str | None = detail
        self.editable: bool = can_edit if editable is None else bool(editable)

        value = deepcopy(self.leaf.get("value"))
        self._initial = value
        self._value = deepcopy(value)
        self._backend_dirty = bool(self.leaf.get("dirty", False))
        self._error: str | None = None
        self._listeners: list[Callable[[LeafBinder, BinderEvent], None]] = []

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    @property
    def node(self) -> tuple[str, str]:
        """外部通知的寻址键 ``(section, config_key)``。"""
        return str(self.leaf["section"]), str(self.leaf["config_key"])

    @property
    def value(self) -> Any:
        """当前值（typed；外部通知与用户编辑都改它）。"""
        return self._value

    @property
    def initial(self) -> Any:
        """初值快照（最近一次「权威状态」的值）。"""
        return self._initial

    @property
    def default(self) -> Any:
        """声明默认值（「重置」按钮用）。"""
        return self.leaf.get("default")

    @property
    def present(self) -> bool:
        """配置里是否真的写了这个键。"""
        return bool(self.leaf.get("present", True))

    @property
    def user_dirty(self) -> bool:
        """当前值是否偏离初值快照。"""
        return not _same(self._value, self._initial)

    @property
    def backend_dirty(self) -> bool:
        """写回层是否还有没落盘的内容（dump 的 ``dirty``）。"""
        return self._backend_dirty

    @property
    def dirty(self) -> bool:
        """视觉用的 dirty：用户改动或待落盘。"""
        return self.user_dirty or self._backend_dirty

    @property
    def error(self) -> str | None:
        """最近一次提交返回的校验错误（行内图标 + tooltip）。"""
        return self._error

    @property
    def is_default(self) -> bool:
        return _same(self._value, self.default)

    # ------------------------------------------------------------------
    # 监听
    # ------------------------------------------------------------------

    def add_listener(
            self, listener: Callable[[LeafBinder, BinderEvent], None],
    ) -> None:
        """登记变更回调（row 与 view 各一份；不做去重）。"""
        self._listeners.append(listener)

    def remove_listener(
            self, listener: Callable[[LeafBinder, BinderEvent], None],
    ) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    def _notify(self, event: BinderEvent) -> None:
        for listener in tuple(self._listeners):
            listener(self, event)

    # ------------------------------------------------------------------
    # 写（视图内部）
    # ------------------------------------------------------------------

    def set_from_ui(self, value: Any) -> bool:
        """用户改动控件：更新当前值 / dirty，返回是否真的变了。

        非可编辑项的调用直接忽略（控件本来就禁用，这里是第二道保险）。
        值真的变化时清除旧的校验错误——错误是对「上一次提交的值」说的。
        """
        if not self.editable:
            return False
        value = deepcopy(value)
        if _same(value, self._value):
            return False
        self._value = value
        if self._error is not None:
            self._error = None
            self._notify("error")
        self._notify("ui")
        return True

    def reset_to_default(self) -> None:
        """「重置」按钮：只改 UI 态，提交时才算数。

        不清 ``backend_dirty``：之前就没落盘的内容不会因为点了重置而消失。
        """
        if not self.editable:
            return
        self._value = deepcopy(self.default)
        if self._error is not None:
            self._error = None
        self._notify("reset")

    # ------------------------------------------------------------------
    # 写（外部通知）
    # ------------------------------------------------------------------

    def set_external(self, value: Any, source: str) -> bool:
        """外部权威更新（``set`` / ``remove`` / ``reset``）。

        - 值、初值快照、``present`` 都换成通知带来的事实；
        - dirty（用户 + 后端）清空、旧错误清空；
        - 通知 row 把控件同步成新值。

        Returns:
            是否真的改变了（用于视图跳过无变化的 dirty 计数刷新）。

        Raises:
            ValueError: ``source`` 不是三种已知来源之一。
            TypeError: ``source`` 是 ``"reload"``——整文件重载走视图的
                :meth:`SettingView.on_reload`，不是单个 binder 的事。
        """
        if source == "reload":
            raise TypeError(
                "reload is a view-level event; call SettingView.on_reload()",
            )
        if source not in ("set", "remove", "reset"):
            raise ValueError(
                f"unknown notification source {source!r}; "
                f"expected set/remove/reset",
            )

        value = deepcopy(value)
        changed = not _same(value, self._value)
        self._value = value
        self._initial = deepcopy(value)
        self._backend_dirty = False
        self.leaf["present"] = source == "set"
        if self._error is not None:
            self._error = None
            changed = True
        self._notify("external")
        return changed

    # ------------------------------------------------------------------
    # 提交结果
    # ------------------------------------------------------------------

    def show_error(self, message: str | None) -> None:
        """把写回层返回的校验错误挂到行内图标（``None`` = 清除）。"""
        text = str(message) if message else None
        if text == self._error:
            return
        self._error = text
        self._notify("error")

    def mark_persisted(self, *, pending: bool = False) -> None:
        """写回结果落定后更新 dirty。

        Args:
            pending: ``True`` 表示这次没落盘（``pending`` / 草稿），保留
                ``backend_dirty``；``False`` 表示已落盘，把当前值当作新初值。
        """
        if pending:
            self._backend_dirty = True
        else:
            self._backend_dirty = False
            self._initial = deepcopy(self._value)
        self._notify("error")

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------

    def collect(self) -> Any:
        """提交用的 typed 值（调用方自己筛 ``editable``）。"""
        return deepcopy(self._value)

    def __repr__(self) -> str:
        flags = []
        if self.editable:
            flags.append("editable")
        if self.dirty:
            flags.append("dirty")
        if self._error:
            flags.append("error")
        return (
            f"<LeafBinder {self.key!r} value={self._value!r} "
            f"{' '.join(flags)}>"
        )
