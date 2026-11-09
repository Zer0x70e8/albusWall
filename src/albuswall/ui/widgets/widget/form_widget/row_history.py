# -*- coding: utf-8 -*-
"""行值历史栈（纯逻辑，不依赖 Qt）。

一句柄一条栈，元素是 ``(raw, action)``：

* ``raw``    —— 只存调用方给的原始值，不做 transform / 校验；
* ``action`` —— 值因何而来：``init`` / ``modify`` / ``restore``，
  仅供调用方（状态点、调试）区分，栈本身不解释它。

规则：**值或 action 任一变化才入栈**；定长（超出丢最老的），只 undo 不 redo。
这段逻辑与 Qt 无关，因此单独成模块，可以脱离界面单测。
"""

from __future__ import annotations

from typing import Any, Optional

#: 历史动作
ACTION_INIT = "init"
ACTION_MODIFY = "modify"
ACTION_RESTORE = "restore"


def values_equal(a: Any, b: Any) -> bool:
    """等值比较：把 None 与「假值」（False / "" / 0 / [] / {}）视为同义。

    这样「初始 raw=None、但复选框实际读到 False」不会被误判成「已修改」。
    """
    if a is b:
        return True
    if a is None:
        return not b
    if b is None:
        return not a
    try:
        return a == b
    except Exception:  # 任意 __eq__ 都可能抛
        return False


class RowHistory:
    """定长、只 undo 的 raw 历史栈。

    :param initial: 初始 raw（对应 ``add_row(value=...)``）
    :param limit:   栈上限，超出时丢最老的条目
    """

    LIMIT = 50

    def __init__(self, initial: Any, limit: int = LIMIT):
        self._limit = max(1, int(limit))
        self._entries: list[tuple[Any, str]] = [(initial, ACTION_INIT)]
        self._index = 0

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    @property
    def index(self) -> int:
        return self._index

    def current(self) -> tuple[Any, str]:
        """当前 ``(raw, action)``。"""
        return self._entries[self._index]

    def current_raw(self) -> Any:
        return self._entries[self._index][0]

    def current_action(self) -> str:
        return self._entries[self._index][1]

    def values(self) -> list[Any]:
        """截至当前位置的全部 raw，``[-1]`` 即当前值。"""
        return [raw for raw, _ in self._entries[: self._index + 1]]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def push(self, raw: Any, action: str = ACTION_MODIFY) -> bool:
        """入栈；值与 action 都没变时返回 ``False``（不入栈）。"""
        cur_raw, cur_action = self._entries[self._index]
        if values_equal(cur_raw, raw) and cur_action == action:
            return False
        del self._entries[self._index + 1:]
        self._entries.append((raw, action))
        self._index += 1
        overflow = len(self._entries) - self._limit
        if overflow > 0:
            del self._entries[:overflow]
            self._index -= overflow
        return True

    def undo(self) -> Optional[tuple[Any, str]]:
        """回退一步并返回回退后的 ``(raw, action)``；已在起点时返回 ``None``。"""
        if not self.can_undo():
            return None
        self._index -= 1
        return self._entries[self._index]

    def can_undo(self) -> bool:
        return self._index > 0

    def can_redo(self) -> bool:
        """留位：本期不实现 redo，恒 False。"""
        return False

    def reset(self, raw: Any, action: str = ACTION_INIT) -> None:
        """清空历史，以 ``raw`` 作为唯一一条起点。"""
        self._entries = [(raw, action)]
        self._index = 0


__all__ = [
    "ACTION_INIT",
    "ACTION_MODIFY",
    "ACTION_RESTORE",
    "RowHistory",
    "values_equal",
]
