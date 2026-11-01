#
"""``TrackingState`` —— state 层的运行时数据容器（只存源快照）。

为什么单独成类
--------------
旧实现把"源快照 + 任务表 + 在途 seq"塞进同一个 ``SourceServiceState``，
于是任何一处更新路径都要同时改三块，且"看得到源却看不到任务"这类不一致
没有任何类型层面的约束。拆成四层后：

* 源快照（本类，``sources``）—— 归 state 层；
* 任务表 / 在途 seq / 待回收 seq —— 归 runtime 层内部字段。

本类因此**只有** ``sources`` 一个字段，不再暴露 ``tasks`` / ``submitted``。

与旧 ``ObservableNamespace`` 的关系
-----------------------------------
旧实现的 ``SourceServiceState`` 继承 ``albuswall.configue.state`` 的
``ObservableNamespace``（``StateField`` 声明 + ``_ctx.mark_dirty`` 触发
observe 回调）。那套基类随旧配置层一起被删除，而新的 ``configue`` 是
**声明式配置树**（见 ``configue/tree.py``：无状态视图 + Resolver 落盘），
语义上是"可序列化的配置值"，与"运行时对象容器"根本不是一类东西 ——
把 ``IngestSource`` 快照塞进配置树只会逼着配置层处理不可序列化值。

所以本类是自带的极简实现，只保留旧基类里**真正有用**的那一小块能力。

契约总表（本类与旧 ``ObservableNamespace`` 的逐条 diff）
--------------------------------------------------------
=========================  ==========================================  ==============================================
契约项                      旧 ``ObservableNamespace``                    本类 ``TrackingState``
=========================  ==========================================  ==============================================
回调签名                     ``cb(node, key: str)``（两个位置参数）        ``cb(state)``（一个位置参数）
key 的含义                  字段名，如 ``"sources"``                      **不再传**：只有 ``sources`` 一个字段
``observe`` 返回值           unsubscribe 闭包                              ``None``；退订请用 :meth:`unobserve`
节点语义                    子命名空间各自标脏，回调收到**变化节点**        只有一个节点，回调恒收到本对象
触发时机                     同步：``__setattr__`` 内立刻回调              同步：写方法内、字段已写入之后立刻回调
回调异常                     静默 ``except Exception: pass``                记 ERROR 日志后继续（不打断写路径）
跨线程安全                   有 RLock，任何线程都能写                       **只允许属主线程**，越界抛 ``RuntimeError``
持久化                       ``StateContext(autosave=True)`` 可落盘         不落盘、不 autosave（值是运行时对象）
=========================  ==========================================  ==============================================

**UI 兼容性**：仓库内目前没有任何订阅方；但如有外部 / 后续 UI 代码按旧
签名 ``lambda node, key: ...`` 订阅，会因参数个数不匹配在首次回调时抛
``TypeError``（不是静默失效）。迁移方式见
``albuswall/service/__init__.py`` 的"迁移期标注"。

线程契约（写路径）
------------------
构造时记录**属主线程**（``threading.get_ident()``），此后
:meth:`mark_dirty` / :meth:`upsert_source` / :meth:`remove_source` /
:meth:`clear` 只在属主线程可用；**非属主线程调用直接抛 ``RuntimeError``**
（早暴露，而不是记 warning 后静默丢状态）。

这是刻意的取舍：state 的读者（UI / 调度）假定"看到的状态永远自洽"，
静默丢弃一次标脏会让观察者永久看不到那次变更，而这种 bug 极难复现
（只在回收站 purge 的工作线程里偶发）。所以宁可在越界的那一刻炸掉。

配套用法：工作线程（``TaskService`` 的 done 回调）不直接写本类 ——
``SourceRuntime`` 只把 seq 记进 ``_settled_seqs``，由主线程的
``reconcile`` / ``rescan`` 末尾统一回收。

外部入口（IPC 命令 / 触发器）不直接碰本类，而是调
``SourceService`` / ``SourceStateService`` 的 public 写方法 —— 那些方法自带
"回主线程"的线程边界（``service/main_loop_service.py``）。本类的属主断言
因此是**兜底防线**：它一旦触发，说明有人绕过了服务边界。
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Iterator, List, Optional, Set

from albuswall.dto.source.query import IngestSource
from albuswall.log import getLogger

__all__ = ["TrackingState", "FIELD_SOURCES"]

_logger = getLogger(__name__)

# 字段名常量：``mark_dirty`` 的入参，避免调用方写裸字符串。
FIELD_SOURCES = "sources"

# 观察者签名：``observe(cb)`` 的 cb 接受本对象本身（不再传 key）。
Observer = Callable[["TrackingState"], None]


class TrackingState:
    """被跟踪源的快照集合（``{source_id: IngestSource}``）。

    只做三件事：存快照、标脏、通知观察者。不做有效性判断、不读仓储、
    不建任务 —— 那些是 :class:`~albuswall.service.source.state.service.SourceStateService`
    的职责。

    完整契约（回调签名 / 触发时机 / 线程归属 / 与旧实现差异）见模块
    docstring；这里只强调两条**会被踩到**的：

    * ``observe(cb)`` 的 ``cb`` 只接受**一个**位置参数（本对象）；
      旧实现的 ``cb(node, key)`` 两参签名已废弃；
    * 所有写方法（含 ``mark_dirty``）只能在**构造该对象的线程**里调用，
      否则抛 ``RuntimeError``。
    """

    __slots__ = ("_sources", "_observers", "_owner_thread", "_owner_name")

    def __init__(
            self,
            sources: Optional[Dict[int, IngestSource]] = None,
            *,
            observers: Optional[List[Observer]] = None,
    ) -> None:
        self._sources: Dict[int, IngestSource] = dict(sources or {})
        self._observers: List[Observer] = list(observers or [])
        # 属主线程：在此记录，此后所有写路径都必须是同一线程。
        self._owner_thread: int = threading.get_ident()
        self._owner_name: str = threading.current_thread().name

    # ------------------------------------------------------------------ 属主
    @property
    def owner_thread_id(self) -> int:
        """构造本对象时的线程 ident（诊断 / 测试用）。"""
        return self._owner_thread

    @property
    def owner_thread_name(self) -> str:
        return self._owner_name

    def _assert_owner(self, operation: str) -> None:
        """写路径的统一守卫：越界调用抛 ``RuntimeError``（见模块 docstring）。"""
        if threading.get_ident() == self._owner_thread:
            return
        raise RuntimeError(
            f"TrackingState.{operation}() called from "
            f"{threading.current_thread().name!r}, but this state belongs to "
            f"{self._owner_name!r}; runtime state must only be mutated on its "
            f"owner thread (工作线程请先把 seq 交给 runtime 的 "
            f"_settled_seqs，由主线程回收)"
        )

    # ------------------------------------------------------------------ 视图
    @property
    def sources(self) -> Dict[int, IngestSource]:
        """底层 dict（只读视图请用 ``SourceStateService.tracked_sources``）。"""
        return self._sources

    def __len__(self) -> int:
        return len(self._sources)

    def __iter__(self) -> Iterator[int]:
        return iter(self._sources)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._sources

    @property
    def source_ids(self) -> Set[int]:
        """被跟踪 id 的副本（调用方改它不会影响内部表）。"""
        return set(self._sources)

    def get(self, source_id: int) -> Optional[IngestSource]:
        """读取快照；未跟踪返回 ``None``。"""
        return self._sources.get(source_id)

    def require_source(self, source_id: int) -> IngestSource:
        """读取被跟踪源的快照；不存在时抛 ``KeyError``。

        只读访问器：``sources`` 的写路径必须走 :meth:`upsert_source` /
        :meth:`remove_source`，读路径收敛到这里，避免调用方散落
        ``self.sources[...]``。
        """
        return self._sources[source_id]

    # ------------------------------------------------------------------ 变更
    def upsert_source(self, source_id: int, source: IngestSource) -> None:
        """新增 / 覆盖一条源快照，并广播 ``sources`` 变更。"""
        self._assert_owner("upsert_source")
        self._sources[source_id] = source
        self.mark_dirty(FIELD_SOURCES)

    def remove_source(self, source_id: int) -> None:
        """移除一条源快照；确实删掉了才标脏（避免空通知）。"""
        self._assert_owner("remove_source")
        if self._sources.pop(source_id, None) is not None:
            self.mark_dirty(FIELD_SOURCES)

    def clear(self) -> None:
        """清空跟踪集；原本为空时不标脏。"""
        self._assert_owner("clear")
        if self._sources:
            self._sources.clear()
            self.mark_dirty(FIELD_SOURCES)

    # ------------------------------------------------------------------ 观察
    def observe(self, callback: Observer) -> None:
        """订阅变更：``cb(state: TrackingState) -> None``。

        * **同步**回调：在写方法内、字段已经写入之后立刻执行；
        * 重复订阅同一 callback 只生效一次；
        * 回调异常只记日志，不打断写路径；
        * 返回 ``None`` —— 退订请调用 :meth:`unobserve`（旧
          ``ObservableNamespace.observe`` 返回的是 unsubscribe 闭包）。
        """
        if callback not in self._observers:
            self._observers.append(callback)

    def unobserve(self, callback: Observer) -> None:
        """退订；未订阅过时静默返回（幂等）。"""
        try:
            self._observers.remove(callback)
        except ValueError:
            pass

    def mark_dirty(self, *fields: str) -> None:
        """标脏并**同步**通知观察者（旧 ``_ctx.mark_dirty`` 的替代）。

        ``fields`` 目前只有 ``"sources"`` 一个取值，保留可变参数是为了与旧
        调用点（``mark_dirty(self, "tasks_by_source")``）的形态兼容 ——
        任务表已搬到 runtime，那边不再需要通知；字段名也**不再**传给回调
        （见模块 docstring 的 diff 表）。

        Raises:
            RuntimeError: 在非属主线程调用（早暴露，见模块 docstring）。
        """
        self._assert_owner("mark_dirty")
        del fields  # 目前只有 sources 一个字段；接口保留以便后续扩展
        for callback in list(self._observers):
            try:
                callback(self)
            except Exception as exc:  # noqa: BLE001 —— 观察者异常不打断写路径
                _logger.exception("tracking observer raised: %s", exc)

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = f"TrackingState(sources={len(self._sources)})"
        if verbose:
            details = ", ".join(
                f"{sid}->{s.source_path!r}" for sid, s in self._sources.items()
            )
            base = base[:-1] + f", sources={{{details}}})"
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")
