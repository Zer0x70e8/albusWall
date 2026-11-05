#
"""配置变更广播：``EventHub``。

为什么要单独一层
================

``Resolver`` 已经有一把 ``RLock`` 管住全部共享状态（``_raw`` / ``_origins`` /
``_definitions`` / ``_file_raw`` / 缓存）。如果订阅表也塞进去，回调里再
``set()`` 一次就变成「锁中锁 + 重入改表」，一条事件路径上要同时保证
「快照」「不丢事件」「不死锁」三件事。

所以拆成这个**独立的小对象**：

- ``Resolver`` 只持有一个 ``events`` 字段，在**锁外**调 :meth:`EventHub.emit`；
- ``EventHub`` 自己用一把小锁**只保护订阅表**，``emit`` 期间不持任何锁
  （先快照、后逐个回调），因此回调里随便 ``set`` / ``subscribe`` /
  ``unsubscribe`` 都不会死锁；
- 单个订阅者抛异常被 ``logger.exception`` 吃掉，不影响其它订阅者，也不
  冒泡到 ``set`` 的调用方 —— 广播是**旁路**，不能影响写配置的成败。

语义
====

================  ==================================================
``source``        触发点
================  ==================================================
``"set"``         ``Resolver.set``（运行期赋值 / 树视图写入）
``"remove"``      ``Resolver.remove``（运行期删值）
``"reset"``       ``Resolver.reset``（恢复默认：回退到低优先级文件或消失）
``"reload"``      ``Resolver.inject_raw`` **且值确实变化**（``load()`` 重读）
================  ==================================================

``flush()`` **不广播**：它只落盘，不改变任何值。

本模块不 import Qt、不 import 任何业务层，只依赖标准库。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any, Literal, Optional

__all__ = ["DEFAULT_SOURCES", "EventHub", "Node", "Source"]

logger = logging.getLogger(__name__)

#: 配置节点：``(section, key)``。
Node = tuple[str, str]

#: 广播来源标记（见模块 docstring 的对照表）。
Source = Literal["set", "remove", "reset", "reload"]

DEFAULT_SOURCES: tuple[str, ...] = ("set", "remove", "reset", "reload")

#: 订阅回调签名：``callback(node, value, source)``。
Callback = Callable[[Node, Any, str], None]


def _freeze_nodes(nodes: Optional[Iterable[Any]]) -> Optional[frozenset[Node]]:
    """把 ``nodes`` 参数规范化成 ``frozenset[Node]``；``None`` 原样返回。

    接受三种写法（都是同一个意思）：

    - ``None`` → 订阅**全部**节点；
    - ``("section", "key")`` —— 单个节点（2 个字符串的元组，最常见的误写）；
    - ``[("s", "k1"), ("s", "k2")]`` —— 任意可迭代的节点集合。

    空的可迭代对象 = 「只订阅零个节点」（不会退化成「全部」——那是两种完全
    不同的意图）。
    """
    if nodes is None:
        return None
    if (
            isinstance(nodes, tuple)
            and len(nodes) == 2
            and all(isinstance(part, str) for part in nodes)
    ):
        return frozenset({(nodes[0], nodes[1])})
    frozen: set[Node] = set()
    for node in nodes:
        if isinstance(node, tuple) and len(node) == 2:
            frozen.add((str(node[0]), str(node[1])))
            continue
        raise TypeError(
            "subscribe(nodes=...): each node must be a (section, key) tuple, "
            f"got {node!r}"
        )
    return frozenset(frozen)


class EventHub:
    """订阅表 + 广播；**不持有**任何配置状态。

    线程语义：``subscribe`` / ``emit`` / ``clear`` 可在任意线程调用。订阅表
    的增删在内部锁下完成，``emit`` 只在**取快照**时短暂持锁，回调一律在锁外
    执行。

    幂等：同一 ``(callback, nodes)`` 重复 ``subscribe`` 只入队一次；返回的
    取消函数可重复调用（第二次是 no-op）。
    """

    __slots__ = ("_subs", "_lock")

    def __init__(self) -> None:
        #: ``[(callback, frozenset[Node] | None), ...]``，插入顺序即回调顺序。
        self._subs: list[tuple[Callback, Optional[frozenset[Node]]]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(
            self,
            callback: Callback,
            *,
            nodes: Optional[Iterable[Any]] = None,
    ) -> Callable[[], None]:
        """登记一个订阅者，返回**幂等**的取消函数。

        Args:
            callback: ``callback(node, value, source)``。抛异常会被隔离
                （``logger.exception``），不影响其它订阅者。
            nodes: ``None`` 订阅全部；可迭代时只收这些节点的事件。

        Returns:
            取消函数，重复调用不抛。
        """
        if not callable(callback):
            raise TypeError(
                f"subscribe: callback must be callable, "
                f"got {type(callback).__name__}"
            )
        watch = _freeze_nodes(nodes)
        entry = (callback, watch)
        with self._lock:
            # 同一 (callback, nodes) 不重复入队；返回的取消函数对两者都生效。
            for existing in self._subs:
                if existing == entry:
                    break
            else:
                self._subs.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove(entry)
                except ValueError:
                    pass  # 已经取消过了：幂等

        return unsubscribe

    # ------------------------------------------------------------------
    # 广播
    # ------------------------------------------------------------------
    def emit(self, node: Node, value: Any, *, source: str) -> None:
        """把一次变更通知给匹配的订阅者（锁外执行回调）。

        Args:
            node: 变更的 ``(section, key)``。
            value: 变更后的值（``remove`` / ``reset`` 到「节点消失」时为
                ``None``）。
            source: ``set`` / ``remove`` / ``reset`` / ``reload`` 之一
                （不做强校验：将来加新来源不该需要改这里）。
        """
        key = (str(node[0]), str(node[1]))
        with self._lock:
            snapshot = tuple(self._subs)
        for callback, watch in snapshot:
            if watch is not None and key not in watch:
                continue
            try:
                callback(key, value, source)
            except Exception:  # noqa: BLE001  广播是旁路，不允许影响写配置
                logger.exception(
                    "config event subscriber %r failed on %s (%s)",
                    callback, key, source,
                )

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """清空全部订阅者（``Configue.close()`` 调用，防泄漏）。"""
        with self._lock:
            self._subs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"<EventHub subscribers={len(self)}>"
