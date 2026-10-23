#
"""进程内 / 跨进程信号系统。

======================================================================
权威契约
======================================================================

本模块**只承认一种**回调签名::

    Signal:        cb(sender,    **kwargs)
    ProcessSignal: cb(sender_id, **kwargs)

- 第一个位置参数永远是 **sender**（进程内信号是任意对象，跨进程信号
  是可 pickle 的稳定标识）。
- 所有 payload 必须通过 **kwargs** 传递，禁止作为第二个位置参数。
- 接收端不关心 sender 时，用 ``_sender`` / ``_`` 命名占位，让"我不关心"
  在签名层面可见。

以下写法**违反契约**（会触发 connect 时的签名预检 / emit 时的 debug 诊断）::

    sig.emit(payload)              # ❌ 只传位置参数：读起来像 payload，
                                   #    实际却成了 sender；语义不明
    sig.emit(self, asset_uuid)     # ❌ payload 不能是第二个位置参数

以下写法是**正确**的::

    sig.emit(self, **totals)                # ✅
    sig.emit(self, asset_uuid=asset_uuid)   # ✅
    sig.emit(result)                        # ✅ 但语义是"result 就是 sender"
                                            #    接收端: def cb(sender): ...

======================================================================
与 Qt 信号槽的关键差异
======================================================================

本实现借鉴了 Qt 的信号槽理念（类属性声明、松耦合观察者），但**没有
照搬** Qt 的表皮契约，原因在于两者地基不同：

- Qt 的 ``emit`` **不接受** sender；需要 sender 时用 ``sender()``
  反射获取（官方不推荐）。本实现**显式**把 sender 作为第一个位置
  参数传给每个 slot——透明、类型可控、跨进程也能拿到 ``sender_id``。
- Qt 有 ``Direct / Queued / Auto`` 连接类型（解决 QObject 的线程
  亲和性问题）。本实现没有事件循环的概念：``Signal`` 同步派发，
  ``ProcessSignal`` 用独立监听线程 + Manager 队列；引入连接类型
  只会制造无法兑现的语义。
- Qt 的 ``@pyqtSlot`` 是为 PyQt 封装 C++ Qt 时的线程亲和性陷阱而
  存在的；本实现是纯 Python，不需要装饰器，``connect`` 时做签名
  预检已足够。

======================================================================
速查
======================================================================

进程内信号（线程安全，同步派发）::

    sig = Signal("scan")
    sig.connect(lambda sender, path: print(sender, path))
    sig.emit(self, path="/tmp")
    sig.disconnect(slot)            # 可选

跨进程广播（同名信号跨所有进程）::

    # 主进程：初始化一次
    if __name__ == "__main__":
        scan = ProcessSignal("scan")
        scan.connect(lambda sender_id, path: print(sender_id, path))

        p = multiprocessing.Process(target=worker, args=(scan,))
        p.start()
        p.join()

    # 子进程
    def worker(scan):
        scan.emit(sender_id="worker-1", path="/tmp")

======================================================================
限制
======================================================================

· ProcessSignal 的总线必须在主进程里初始化一次
  （在 `if __name__ == "__main__":` 里创建，或调用 get_manager()）。
· 子进程通过 fork 继承总线。Windows 的 spawn 模式需要额外处理。
· 同名 ProcessSignal 在本进程内共享一组回调。

======================================================================
迁移指引
======================================================================

1. ``send()`` 已进入弃用期，全部替换为 ``emit()``；
   测试环境可打开 ``-W error::DeprecationWarning`` 一次性扫清。
2. 类属性上的信号一律用 ``SignalDescriptor``；``Signal(...)`` 只用于
   实例属性或模块级单例（详见 SignalDescriptor 的 docstring）。
3. 每个信号在其声明处用 docstring 写明契约
   （``cb(sender, *, ...)``）。
"""

from __future__ import annotations

import inspect
import logging
import multiprocessing
import os
import pickle
import queue as _queue_mod
import threading
import warnings
from typing import Any, Callable, Optional

_logger = logging.getLogger(__name__)

__all__ = [
    "Signal",
    "ProcessSignal",
    "SignalDescriptor",
    "get_manager",
    "shutdown",
]


# =========================================================================== #
# 内部：slot 签名预检
# =========================================================================== #

def _check_slot_signature(signal_name: str, slot: Callable[..., Any]) -> None:
    """在 connect 时检查 slot 是否能接受至少一个位置参数。

    契约是 ``cb(sender, **kwargs)``——slot 必须能接收 sender 这个位置
    参数。若完全无法接收位置参数，emit 时必然 ``TypeError``；注册期就
    warn 出来，避免错误延迟到派发时才暴露（甚至只在接收方进程里暴露）。

    只 warn、不阻止注册——保持向后兼容。对于无法反射的内建函数 /
    C 扩展 callable，``inspect.signature`` 会抛异常，此时放行。
    """
    try:
        sig = inspect.signature(slot)
    except (ValueError, TypeError):
        return

    for p in sig.parameters.values():
        if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
        ):
            return

    warnings.warn(
        f"[Signal {signal_name!r}] slot {slot!r} 不接受任何位置参数；"
        f"权威契约是 cb(sender, **kwargs)，emit 时将抛 TypeError",
        UserWarning,
        stacklevel=3,
    )


# =========================================================================== #
# Manager（跨进程总线依赖）
# =========================================================================== #

_manager: Optional[Any] = None
_manager_lock = threading.Lock()


def get_manager() -> Any:
    """获取全局 Manager 单例。

    必须在主进程里首次调用；fork 出的子进程会继承它。
    在没有继承到 Manager 的子进程里调用会抛 RuntimeError。
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                if multiprocessing.current_process().name != "MainProcess":
                    raise RuntimeError(
                        "跨进程总线必须在主进程里初始化。\n"
                        "请在 if __name__ == '__main__': 里创建一次 "
                        "ProcessSignal（或手动调用 get_manager()），再 fork。"
                    )
                _manager = multiprocessing.Manager()
    return _manager


# =========================================================================== #
# 进程内信号
# =========================================================================== #

# noinspection broad-exception
class Signal:
    """进程内线程安全信号。

    **权威契约**::

        cb(sender, **kwargs)

    - ``sender`` —— 触发者（任意对象，可为 ``None``），**永远是第一个位置参数**。
    - ``kwargs`` —— 所有 payload 必须具名。

    **正确**用法::

        sig.emit(self, **totals)                # ✅
        sig.emit(self, asset_uuid=asset_uuid)   # ✅
        sig.emit(result)                        # ✅ 语义是"result 就是 sender"
                                                #    接收端: def cb(sender): ...

    **违禁**用法（connect 时会 warn / emit 时会 debug 提示）::

        sig.emit(payload)              # ❌ 只传位置参数，语义不明
        sig.emit(self, asset_uuid)     # ❌ payload 不能是第二个位置参数

    与 Qt 的差异见模块 docstring。
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._lock = threading.RLock()
        self._slots: list[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        """注册回调；重复注册同一 slot 只生效一次。

        契约：``slot(sender, **kwargs)``。若 slot 签名不接受任何位置
        参数，注册时会发出 ``UserWarning``（不会阻止注册）。
        """
        _check_slot_signature(self.name, slot)
        with self._lock:
            if slot not in self._slots:
                self._slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        """取消注册；未注册过的 slot 静默忽略。"""
        with self._lock:
            try:
                self._slots.remove(slot)
            except ValueError:
                pass

    def emit(self, sender: Any = None, **kwargs: Any) -> None:
        """同步触发所有回调。

        - 单个回调抛异常只记录日志，不影响其他回调。
        - 若只传了位置参数（``sender``）而无 ``kwargs``，会打 debug
          日志提示——这可能是"把 payload 误当 sender"的信号。
        """
        if sender is not None and not kwargs:
            _logger.debug(
                "[Signal %r] emit 只传了位置参数、无 kwargs；"
                "若意图是传 payload，请写成 emit(sender, payload=...)。"
                "权威契约: cb(sender, **kwargs)",
                self.name,
            )

        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(sender, **kwargs)
            except Exception:
                _logger.exception(
                    "[Signal %s] slot %r 执行失败", self.name, slot,
                )

    def send(self, *args: Any, **kwargs: Any) -> None:
        """已废弃——请改用 :meth:`emit`。"""
        warnings.warn(
            f"[Signal {self.name!r}] send() 已废弃，请改用 emit()",
            DeprecationWarning,
            stacklevel=2,
        )
        self.emit(*args, **kwargs)

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._slots)
        return f"<Signal {self.name!r} slots={n}>"


# =========================================================================== #
# 跨进程总线（模块级状态，fork 继承）
# =========================================================================== #
#
# 本进程的入站队列：{pid: Queue}，由 _get_bus() 懒创建并共享给所有
# 同名 / 不同名的 ProcessSignal。每个进程只读自己的队列。
#
_my_queue: Optional[Any] = None
_my_queue_pid: Optional[int] = None
_my_queue_lock = threading.Lock()

_bus: Optional[Any] = None  # manager.dict(): "pid:N" -> Queue
_bus_lock = threading.Lock()

_listener: Optional[threading.Thread] = None
_listener_pid: Optional[int] = None
_listener_lock = threading.Lock()

# topic -> 回调列表；同名 ProcessSignal 在本进程内共享
_handlers: dict[str, list[Callable[..., Any]]] = {}
_handlers_lock = threading.RLock()

_shutdown_event = threading.Event()


def _get_bus() -> Any:
    """懒创建全局总线（一次，之后 fork 继承）。

    返回类型是 ``Any``（非 Optional）：调用方拿到的**一定**是已初始化的
    总线对象。
    """
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = get_manager().dict()
    # 显式断言，向类型检查器保证非 None（模块级全局在窄化后不可靠）
    assert _bus is not None
    return _bus


def _get_my_queue() -> Any:
    """获取本进程的入站队列，并在总线上注册。

    通过 pid 判断 fork：子进程继承来的 ``_my_queue`` 属于父进程，
    必须换成自己的，否则会和父进程抢消息。

    返回类型是 ``Any``（非 Optional）。
    """
    global _my_queue, _my_queue_pid
    my_pid = os.getpid()

    # 先用局部变量承接，避免 PyCharm 对全局的窄化不信任
    q = _my_queue
    if q is not None and _my_queue_pid == my_pid:
        return q

    with _my_queue_lock:
        q = _my_queue
        if q is None or _my_queue_pid != my_pid:
            new_q = get_manager().Queue()
            bus = _get_bus()  # 返回 Any，__setitem__ 不再被报 None
            bus[f"pid:{my_pid}"] = new_q
            _my_queue = new_q
            _my_queue_pid = my_pid
            q = new_q

    assert q is not None
    return q


def _ensure_listener() -> None:
    """确保本进程的监听线程已启动（fork 后重启）。"""
    global _listener, _listener_pid
    my_pid = os.getpid()
    if (
            _listener_pid == my_pid
            and _listener is not None
            and _listener.is_alive()
    ):
        return

    with _listener_lock:
        if (
                _listener_pid == my_pid
                and _listener is not None
                and _listener.is_alive()
        ):
            return
        # 用局部变量承接新建的线程，调用方直接对局部变量 start()，
        # 避免对全局 Optional[Thread] 的窄化问题
        listener = threading.Thread(
            target=_dispatch_loop,
            name=f"ProcessSignal-listener[pid={my_pid}]",
            daemon=True,
        )
        _listener = listener
        _listener_pid = my_pid
        listener.start()


# noinspection broad-exception
def _dispatch_loop() -> None:
    """从本进程的队列拉消息，按 topic 分派。"""
    q = _get_my_queue()  # 返回 Any，非 Optional
    while not _shutdown_event.is_set():
        try:
            msg = q.get(timeout=0.5)
        except _queue_mod.Empty:
            continue
        except (EOFError, OSError):
            break
        except Exception:
            _logger.exception("[ProcessSignal] 拉取消息失败")
            continue

        if msg is None:
            break

        try:
            topic, sender_id, kwargs = msg
        except (ValueError, TypeError):
            _logger.warning("[ProcessSignal] 未知消息: %r", msg)
            continue

        with _handlers_lock:
            slots = list(_handlers.get(topic, ()))

        for slot in slots:
            try:
                slot(sender_id, **kwargs)
            except Exception:
                _logger.exception(
                    "[ProcessSignal %s] slot %r 执行失败", topic, slot,
                )


# noinspection broad-exception
def shutdown() -> None:
    """停止本进程的监听线程（幂等）。"""
    _shutdown_event.set()
    q = _my_queue  # 局部变量承接，窄化可靠
    if q is not None:
        try:
            # noinspection unresolved-references
            q.put(None)
        except Exception:
            pass


# =========================================================================== #
# 跨进程信号
# =========================================================================== #

# noinspection broad-exception
class ProcessSignal:
    """跨进程广播信号。

    **权威契约**::

        cb(sender_id, **kwargs)

    - ``sender_id`` —— **可 pickle 的稳定标识**（建议用字符串，
      如 ``"worker-1"``）。发送方和所有接收方拿到的是同一个值。
    - ``kwargs`` —— 必须可 pickle。所有 payload 必须具名。

    同名信号在本进程内共享回调；不同进程通过全局总线广播。

    **前置条件**：总线必须在主进程里初始化一次。最简单的做法是
    在 ``if __name__ == "__main__":`` 里创建一次 ``ProcessSignal``，
    再 fork 子进程。

    示例::

        # 主进程
        if __name__ == "__main__":
            scan = ProcessSignal("scan")
            scan.connect(lambda sender_id, path: print(sender_id, path))

            p = multiprocessing.Process(target=worker, args=(scan,))
            p.start()
            p.join()

        # 子进程
        def worker(scan):
            scan.emit(sender_id="worker-1", path="/tmp")
    """

    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("ProcessSignal 需要一个非空 name")
        self.name = name

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def connect(self, slot: Callable[..., Any]) -> None:
        """注册回调；首次调用会自动拉起本进程的接收队列和监听线程。

        契约：``slot(sender_id, **kwargs)``。签名预检同 ``Signal.connect``。
        """
        _check_slot_signature(self.name, slot)
        _get_my_queue()
        _ensure_listener()
        with _handlers_lock:
            slots = _handlers.setdefault(self.name, [])
            if slot not in slots:
                slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        """取消注册。"""
        with _handlers_lock:
            slots = _handlers.get(self.name)
            if slots and slot in slots:
                slots.remove(slot)

    # ------------------------------------------------------------------ #
    # 触发
    # ------------------------------------------------------------------ #
    # noinspection unresolved-references
    def emit(self, sender_id: Any, **kwargs: Any) -> None:
        """触发信号：本地同步执行 + 广播到其他进程。

        ``sender_id`` 和 ``kwargs`` 都必须可 pickle；不可 pickle 时
        本地回调仍会执行，跨进程广播会被跳过并记日志。

        若 ``kwargs`` 为空，会打 debug 日志提示——常见于"仅通知事件
        发生、不带 payload"的合法用法，也可能是不小心漏了 payload。
        """
        if not kwargs:
            _logger.debug(
                "[ProcessSignal %r] emit 未携带 payload kwargs；"
                "若这是有意的通知，请忽略本提示。契约: cb(sender_id, **kwargs)",
                self.name,
            )

        # 1) 本地立即执行
        with _handlers_lock:
            slots = list(_handlers.get(self.name, ()))
        for slot in slots:
            try:
                slot(sender_id, **kwargs)
            except Exception:
                _logger.exception(
                    "[ProcessSignal %s] slot %r 执行失败", self.name, slot,
                )

        # 2) 广播
        bus = _bus
        if bus is None:  # 没有任何进程订阅
            return

        try:
            pickle.dumps((sender_id, kwargs))
        except Exception:
            _logger.exception(
                "[ProcessSignal %s] 参数不可 pickle，跳过跨进程广播",
                self.name,
            )
            return

        my_key = f"pid:{os.getpid()}"
        msg = (self.name, sender_id, kwargs)
        for key, q in list(bus.items()):
            if key == my_key:  # 不给自己发，本地已执行
                continue
            try:
                q.put(msg)
            except Exception:
                _logger.debug(
                    "[ProcessSignal %s] 广播到 %s 失败", self.name, key,
                )

    def send(self, *args: Any, **kwargs: Any) -> None:
        """已废弃——请改用 :meth:`emit`。"""
        warnings.warn(
            f"[ProcessSignal {self.name!r}] send() 已废弃，请改用 emit()",
            DeprecationWarning,
            stacklevel=2,
        )
        self.emit(*args, **kwargs)

    def __call__(self, sender_id: Any, **kwargs: Any) -> None:
        self.emit(sender_id, **kwargs)

    def __repr__(self) -> str:
        with _handlers_lock:
            n = len(_handlers.get(self.name, ()))
        return f"<ProcessSignal {self.name!r} slots={n}>"


# =========================================================================== #
# 类属性声明
# =========================================================================== #

class SignalDescriptor:
    """把信号声明为类属性；由**声明类**及其子类共享同一个实例。

    参数：
        cross_process=False → :class:`Signal`
        cross_process=True  → :class:`ProcessSignal`

    **边界（很重要）**：

    只要信号声明在**类体**里、作为类属性存在，就**必须**用
    ``SignalDescriptor``——不要写成 ``attr: Signal = Signal(...)``。原因：

    - ``Signal(...)`` 在类定义时就**立即**构造实例，无法 lazy；
      而 ``ProcessSignal(...)`` 作为类属性会在 **import 时**就尝试
      初始化 Manager，在非主进程的 import 路径里会直接炸。
    - ``SignalDescriptor`` 天然 lazy，且语义清晰——"这是该类的共享
      信号"。
    - 作为类属性、实例之间天然共享的语义，应当由描述符承担；
      ``Signal`` / ``ProcessSignal`` 本身是"信号实例"，不是"信号
      属性"。

    ``Signal(...)`` / ``ProcessSignal(...)`` 只用于：

    - 实例属性（在 ``__init__`` 里创建，实例独占）；
    - 模块级单例（模块顶层创建一次，全局共享）。

    示例::

        class Service:
            scan_finished = SignalDescriptor()
            source_added  = SignalDescriptor(cross_process=True)

            # 契约写在声明处；接收端签名是 cb(sender, *, ...)
            # scan_finished:  cb(sender: Service, *, path: str)
            # source_added:   cb(sender_id: str, *, source_id: str)

        Service.scan_finished.connect(cb)   # 所有实例 / 子类共享
        Service.scan_finished.emit(svc, path="/tmp")
    """

    def __init__(self, name: str = "", *, cross_process: bool = False) -> None:
        self.name = name
        self.cross_process = cross_process
        self._signals: dict[type, Any] = {}
        self._lock = threading.RLock()
        self._owner: Optional[type] = None

    def __set_name__(self, owner: type, name: str) -> None:
        if not self.name:
            self.name = name
        self._owner = owner

    def __get__(self, instance: Any, owner: type) -> Any:
        # 始终以"声明类"为 key：子类继承父类的信号，
        # 不会因为访问 SubClass.foo 而造出一份新信号。
        cls = self._owner if self._owner is not None else owner
        with self._lock:
            sig = self._signals.get(cls)
            if sig is None:
                sig = (
                    ProcessSignal(self.name)
                    if self.cross_process
                    else Signal(self.name)
                )
                self._signals[cls] = sig
            return sig
