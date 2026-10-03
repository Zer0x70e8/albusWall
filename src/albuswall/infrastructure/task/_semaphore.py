#
"""动态信号量：支持运行时调整上限，仅供调度器使用。

不属于公共契约。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from albuswall.log import getLogger

_logger = getLogger(__package__)


class DynamicSemaphore:
    """
    支持动态调整最大 permit 数的信号量。

    关键语义:
      - ``_current`` 记录当前已持有 permit 的数量
      - ``_max`` 是当前上限，被限制在 [_min, _hard_max] 之间
      - ``acquire`` 阻塞直到 ``_current < _max``
      - ``release`` 只递减 ``_current``，不会让 ``_current`` 超过 ``_max``
      - ``decrease_max`` 之后，``_current`` 可能暂时大于 ``_max``。
        此时 ``available`` 返回 0，``acquire`` 会阻塞，直到有 permit 释放。
        这是刻意行为，不是 bug。
    """

    def __init__(self, initial: int,
                 min_value: int = 1,
                 max_value: Optional[int] = None):
        if initial < min_value:
            initial = min_value
        if max_value is not None and initial > max_value:
            initial = max_value

        self._cond = threading.Condition()
        self._current = 0
        self._max = initial
        self._min = max(0, min_value)
        self._hard_max = max_value if max_value is not None else initial

    # ---------- acquire / release ----------
    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if not blocking:
                if self._current < self._max:
                    self._current += 1
                    return True
                return False

            if timeout is None:
                while self._current >= self._max:
                    self._cond.wait()
                self._current += 1
                return True

            deadline = time.monotonic() + timeout
            while self._current >= self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._current += 1
            return True

    def release(self):
        with self._cond:
            if self._current > 0:
                self._current -= 1
                self._cond.notify()
            else:
                _logger.warning("DynamicSemaphore.release() called with _current == 0")

    # ---------- dynamic adjust ----------
    def increase_max(self, n: int = 1) -> int:
        """提高上限（受 hard_max 限制），返回实际增量。"""
        with self._cond:
            old = self._max
            self._max = min(self._hard_max, self._max + n)
            if self._max != old:
                self._cond.notify_all()
            return self._max - old

    def decrease_max(self, n: int = 1) -> int:
        """降低上限（受 _min 限制），返回实际减少量。"""
        with self._cond:
            old = self._max
            self._max = max(self._min, self._max - n)
            if self._max != old:
                self._cond.notify_all()
            return old - self._max

    # ---------- properties ----------
    @property
    def max(self) -> int:
        return self._max

    @property
    def min(self) -> int:
        return self._min

    @property
    def hard_max(self) -> int:
        return self._hard_max

    @property
    def current(self) -> int:
        return self._current

    @property
    def available(self) -> int:
        return max(0, self._max - self._current)
