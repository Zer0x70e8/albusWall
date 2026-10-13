"""LRU cache supporting both entry-count and cost-based capacity limits,
plus a functools-style decorator built on top of it.
"""

from collections import OrderedDict
from typing import (
    TypeVar,
    Any, Callable, Generic, Iterator, NamedTuple, Optional
)

import functools
from contextlib import nullcontext
from threading import RLock

_UNHASHABLE = object()
K = TypeVar("K")
V = TypeVar("V")


class LRUCache(Generic[K, V]):
    """LRU cache with two mutually exclusive capacity modes:

    - **By entry count** (default): ``LRUCache(capacity=200)``
    - **By cost**: ``LRUCache(max_cost=256 * 1024 * 1024,
                               cost_of=lambda pm: pm.width() * pm.height() * 4)``

    ``max_cost`` requires ``cost_of``; ``capacity`` must not be combined
    with ``cost_of``.
    """

    def __init__(
            self,
            capacity: Optional[int] = None,
            *,
            max_cost: Optional[int] = None,
            cost_of: Optional[Callable[[V], int]] = None,
    ):
        if (capacity is None) == (max_cost is None):
            raise ValueError("exactly one of capacity or max_cost must be given")

        if capacity is not None:
            if capacity < 1:
                raise ValueError("capacity must be greater than 0")
            self._capacity = capacity
            self._max_cost = None
            self._cost_of = None
        else:
            if cost_of is None:
                raise ValueError("cost-based mode requires cost_of")
            if max_cost < 1:
                raise ValueError("max_cost must be greater than 0")
            self._capacity = None
            self._max_cost = max_cost
            self._cost_of = cost_of

        self._cache: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._total_cost = 0

    # Read-only views for external access (and for the decorator layer),
    # so callers never need to touch private attributes.

    @property
    def capacity(self) -> Optional[int]:
        return self._capacity

    @property
    def max_cost(self) -> Optional[int]:
        return self._max_cost

    @property
    def cost_of(self) -> Optional[Callable[[V], int]]:
        return self._cost_of

    @property
    def total_cost(self) -> int:
        return self._total_cost

    # Basic interface

    def __getitem__(self, key: K) -> V:
        value, _ = self._cache[key]
        self._cache.move_to_end(key)
        return value

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Return ``default`` instead of raising ``KeyError`` on a miss."""
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

    def __iter__(self) -> Iterator[K]:
        return iter(self._cache)

    def remove(self, key: K) -> None:
        item = self._cache.pop(key, None)
        if item is not None:
            self._total_cost -= item[1]

    def clear(self) -> None:
        self._cache.clear()
        self._total_cost = 0

    # Internals

    def _evict(self) -> None:
        if self._max_cost is not None:
            while self._total_cost > self._max_cost and self._cache:
                _, (_, cost) = self._cache.popitem(last=False)
                self._total_cost -= cost
        else:
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)


def _make_key(args: tuple, kwargs: dict, typed: bool) -> Any:
    """Build a cache key; return ``_UNHASHABLE`` so the caller skips caching
    when an argument cannot be hashed."""
    key: tuple = args
    if kwargs:
        key += tuple(sorted(kwargs.items()))
    if typed:
        key += tuple(type(a) for a in args)
        if kwargs:
            key += tuple(type(v) for _, v in sorted(kwargs.items()))
    try:
        hash(key)
    except TypeError:
        return _UNHASHABLE
    return key


class CacheInfo(NamedTuple):
    """Mirrors ``functools.lru_cache``'s CacheInfo, with cost fields added.

    Field names follow ``functools.lru_cache`` spelling.
    """
    hits: int
    misses: int
    currsize: int  # noqa: typo  -- matches functools.lru_cache
    maxsize: int
    currcost: int = -1  # noqa: typo
    maxcost: int = -1  # noqa: typo


# noinspection SpellCheckingInspection
class LRUCached(Generic[K, V]):
    """Decorator-style LRU cache for functions.

    Composition instead of inheritance:
    - ``self._cache`` holds the underlying ``LRUCache`` instance
    - underlying interface is forwarded via properties / methods
    - hits / misses counters and an optional thread lock live here
    """

    def __init__(
            self,
            func: Callable[..., V],
            *,
            capacity: Optional[int] = None,
            max_cost: Optional[int] = None,
            cost_of: Optional[Callable[[V], int]] = None,
            typed: bool = False,
            lock: bool = True,
    ) -> None:
        # Set own attributes before update_wrapper, so they are not
        # overwritten by func.__dict__.
        self._func: Callable[..., V] = func
        self._cache: LRUCache[Any, V] = LRUCache(
            capacity=capacity,
            max_cost=max_cost,
            cost_of=cost_of,
        )
        self._typed = typed
        self._hits = 0
        self._misses = 0
        self._lock: Optional[RLock] = RLock() if lock else None

        functools.update_wrapper(self, func)

    # Call logic

    def __call__(self, *args: Any, **kwargs: Any) -> V:
        key = _make_key(args, kwargs, self._typed)

        # Unhashable arguments: skip the cache and call through.
        if key is _UNHASHABLE:
            with self._maybe_lock():
                self._misses += 1
            return self._func(*args, **kwargs)

        # Hit check
        with self._maybe_lock():
            if key in self._cache:
                self._hits += 1
                return self._cache[key]  # also moves the entry to the end
            self._misses += 1

        # Miss: run the user function without holding the lock,
        # so long-running calls do not block other threads.
        value = self._func(*args, **kwargs)

        with self._maybe_lock():
            self._cache[key] = value
        return value

    def _maybe_lock(self):
        return self._lock if self._lock is not None else nullcontext()

    # Underlying object access

    @property
    def cache(self) -> LRUCache[K, V]:
        """The internal ``LRUCache`` instance, for advanced operations."""
        return self._cache

    @property
    def func(self) -> Callable[..., V]:
        """The wrapped original function."""
        return self._func

    # Forwarded LRUCache config / state

    @property
    def capacity(self) -> Optional[int]:
        return self._cache.capacity

    @property
    def max_cost(self) -> Optional[int]:
        return self._cache.max_cost

    @property
    def cost_of(self) -> Optional[Callable[[V], int]]:
        return self._cache.cost_of

    @property
    def total_cost(self) -> int:
        return self._cache.total_cost

    @property
    def typed(self) -> bool:
        return self._typed

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    # Container protocol

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: object) -> bool:
        return key in self._cache

    def __iter__(self) -> Iterator[K]:
        return iter(self._cache)

    # Mutating operations

    def remove(self, key: K) -> None:
        """Drop a single key (statistics are left untouched)."""
        with self._maybe_lock():
            self._cache.remove(key)

    def clear(self) -> None:
        """Clear the cache but keep hits / misses (mirrors ``LRUCache.clear``)."""
        with self._maybe_lock():
            self._cache.clear()

    # functools-compatible API

    def cache_clear(self) -> None:
        """Clear the cache and reset statistics (mirrors ``functools.lru_cache``)."""
        with self._maybe_lock():
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def cache_info(self) -> CacheInfo:
        with self._maybe_lock():
            # Bind to locals with explicit annotations so the type checker
            # can narrow Optional[int] -> int.
            cap: Optional[int] = self._cache.capacity
            mcost: Optional[int] = self._cache.max_cost
            maxsize: int = cap if cap is not None else -1
            maxcost: int = mcost if mcost is not None else -1

            return CacheInfo(
                hits=self._hits,
                misses=self._misses,
                currsize=len(self._cache),
                maxsize=maxsize,
                currcost=self._cache.total_cost,
                maxcost=maxcost,
            )


def lru_cache(
        capacity: Optional[int] = None,
        *,
        max_cost: Optional[int] = None,
        cost_of: Optional[Callable[[V], int]] = None,
        typed: bool = False,
        lock: bool = True,
) -> Callable[[Callable[..., V]], LRUCached[Any, V]]:
    """Function decorator built on ``LRUCache``.

    Usage::

        @lru_cache(capacity=200)
        def f(x): ...

        @lru_cache(max_cost=256 * 1024 * 1024,
                   cost_of=lambda arr: arr.nbytes)
        def load(path): ...
    """
    if (capacity is None) == (max_cost is None):
        raise ValueError("exactly one of capacity or max_cost must be given")

    def decorator(func: Callable[..., V]) -> LRUCached[Any, V]:
        return LRUCached(
            func,
            capacity=capacity,
            max_cost=max_cost,
            cost_of=cost_of,
            typed=typed,
            lock=lock,
        )

    return decorator
