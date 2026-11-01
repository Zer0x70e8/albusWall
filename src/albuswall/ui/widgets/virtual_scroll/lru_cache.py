#
""""""

from collections import OrderedDict
from typing import TypeVar, Generic, Optional, Callable

K = TypeVar("K")
V = TypeVar("V")


class LRUCache(Generic[K, V]):
    """LRU 缓存，支持两种容量模式：

    - **按条数**（默认）：``LRUCache(capacity=200)``
    - **按成本**：``LRUCache(max_cost=256 * 1024 * 1024,
                            cost_of=lambda pm: pm.width() * pm.height() * 4)``

    两种模式互斥：传了 ``max_cost`` 就必须传 ``cost_of``，
    传了 ``capacity`` 就不能传 ``cost_of``。
    """

    def __init__(
            self,
            capacity: Optional[int] = None,
            *,
            max_cost: Optional[int] = None,
            cost_of: Optional[Callable[[V], int]] = None,
    ):
        if (capacity is None) == (max_cost is None):
            raise ValueError("必须且只能指定 capacity 或 max_cost 之一")

        if capacity is not None:
            if capacity < 1:
                raise ValueError("capacity 必须大于 0")
            self._capacity = capacity
            self._max_cost = None
            self._cost_of = None
        else:
            if cost_of is None:
                raise ValueError("按成本模式必须提供 cost_of")
            if max_cost < 1:
                raise ValueError("max_cost 必须大于 0")
            self._capacity = None
            self._max_cost = max_cost
            self._cost_of = cost_of

        self._cache: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._total_cost = 0

    # ---- 基本接口（保持不变）--------------------------------------------

    def __getitem__(self, key: K) -> V:
        value, _ = self._cache[key]
        self._cache.move_to_end(key)
        return value

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """缺失返回 default 而不是抛 KeyError，方便调用方。"""
        item = self._cache.get(key)
        if item is None:
            return default
        self._cache.move_to_end(key)
        return item[0]

    def __setitem__(self, key: K, value: V) -> None:
        cost = self._cost_of(value) if self._cost_of else 1

        if key in self._cache:
            _, old_cost = self._cache.pop(key)
            self._total_cost -= old_cost

        self._cache[key] = (value, cost)
        self._total_cost += cost

        self._evict()

    def __contains__(self, key: K) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def remove(self, key: K) -> None:
        item = self._cache.pop(key, None)
        if item is not None:
            self._total_cost -= item[1]

    def clear(self) -> None:
        self._cache.clear()
        self._total_cost = 0

    # ---- 内部 ------------------------------------------------------------

    def _evict(self) -> None:
        if self._max_cost is not None:
            while self._total_cost > self._max_cost and self._cache:
                _, (_, cost) = self._cache.popitem(last=False)
                self._total_cost -= cost
        else:
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
