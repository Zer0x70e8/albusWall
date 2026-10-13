#
"""进程内 / 跨进程信号系统。

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
回调签名
======================================================================

    Signal:        cb(sender,    **kwargs)   # sender 是任意对象
    ProcessSignal: cb(sender_id, **kwargs)   # sender_id 必须可 pickle

两者的 sender_id / sender **本地和远程完全一致**。ProcessSignal
不传对象，因为对象不能跨进程；请让调用方传一个稳定字符串。

======================================================================
限制
======================================================================

· ProcessSignal 的总线必须在主进程里初始化一次
  （在 `if __name__ == "__main__":` 里创建，或调用 get_manager()）。
· 子进程通过 fork 继承总线。Windows 的 spawn 模式需要额外处理。
· 同名 ProcessSignal 在本进程内共享一组回调。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import queue as _queue_mod
import threading
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

    回调签名：``cb(sender, **kwargs)``

    示例::

        sig = Signal("scan")
        sig.connect(lambda sender, path: print(sender, path))
        sig.emit(self, path="/tmp")
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._lock = threading.RLock()
        self._slots: list[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        """注册回调；重复注册同一 slot 只生效一次。"""
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

        单个回调抛异常只记录日志，不影响其他回调。
        """
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(sender, **kwargs)
            except Exception:
                _logger.exception(
                    "[Signal %s] slot %r 执行失败", self.name, slot,
                )

    # 别名，兼容旧调用
    send = emit

    def __repr__(self) -> str:
        return f"<Signal {self.name!r} slots={len(self._slots)}>"


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


def _get_bus():
    """懒创建全局总线（一次，之后 fork 继承）。"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = get_manager().dict()
    return _bus


def _get_my_queue():
    """获取本进程的入站队列，并在总线上注册。

    通过 pid 判断 fork：子进程继承来的 ``_my_queue`` 属于父进程，
    必须换成自己的，否则会和父进程抢消息。
    """
    global _my_queue, _my_queue_pid
    my_pid = os.getpid()
    if _my_queue is not None and _my_queue_pid == my_pid:
        return _my_queue

    with _my_queue_lock:
        if _my_queue is None or _my_queue_pid != my_pid:
            q = get_manager().Queue()
            _get_bus()[f"pid:{my_pid}"] = q
            _my_queue = q
            _my_queue_pid = my_pid
    return _my_queue


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
        _listener = threading.Thread(
            target=_dispatch_loop,
            name=f"ProcessSignal-listener[pid={my_pid}]",
            daemon=True,
        )
        _listener_pid = my_pid
        _listener.start()


# noinspection broad-exception
def _dispatch_loop() -> None:
    """从本进程的队列拉消息，按 topic 分派。"""
    q = _get_my_queue()
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

        if msg is None:  # shutdown() 哨兵
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
    """停止本进程的监听线程（幂等）。进程退出时无需手动调用。"""
    _shutdown_event.set()
    if _my_queue is not None:
        try:
            _my_queue.put(None)
        except Exception:
            pass


# =========================================================================== #
# 跨进程信号
# =========================================================================== #

# noinspection broad-exception
class ProcessSignal:
    """跨进程广播信号。

    回调签名：``cb(sender_id, **kwargs)``

    - ``sender_id`` —— **可 pickle 的稳定标识**（建议用字符串，
      如 ``"worker-1"``）。发送方和所有接收方拿到的是同一个值。
    - ``kwargs`` —— 必须可 pickle。

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
        """注册回调；首次调用会自动拉起本进程的接收队列和监听线程。"""
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
    def emit(self, sender_id: Any, **kwargs: Any) -> None:
        """触发信号：本地同步执行 + 广播到其他进程。

        ``sender_id`` 和 ``kwargs`` 都必须可 pickle；不可 pickle 时
        本地回调仍会执行，跨进程广播会被跳过并记日志。
        """
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

    # 别名
    send = emit

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

    ``cross_process=False`` → :class:`Signal`
    ``cross_process=True``  → :class:`ProcessSignal`

    示例::

        class Service:
            scan_finished = SignalDescriptor()
            source_added  = SignalDescriptor(cross_process=True)

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
