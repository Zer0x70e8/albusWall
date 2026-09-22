#
""""""

from collections import OrderedDict


class LRUCache:
    """一个简单的 LRU 缓存，使用 OrderedDict 维护访问顺序。

    当缓存达到容量上限时，会自动淘汰最久未使用的条目。
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity 必须大于 0")
        self.capacity = capacity
        self._cache: OrderedDict = OrderedDict()

    def __getitem__(self, key):
        """获取值并将该键标记为最近使用。"""
        value = self._cache[key]
        self._cache.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        """设置键值对，若键已存在则更新并移至末尾；
        插入新键时如果超出容量，淘汰最旧的键。
        """
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)  # 弹出第一个（最久未使用）

    def __contains__(self, key):
        """成员检查不改变访问顺序（保持只读语义）。"""
        return key in self._cache

    def __len__(self):
        return len(self._cache)

    def clear(self):
        self._cache.clear()
