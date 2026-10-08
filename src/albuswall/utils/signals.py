#
"""跨进程 / 跨线程信号系统。

选型指南
--------
· 同进程、单线程或多线程         → Signal
· 需要跨进程广播                 → ProcessSignal
  （须在主进程 __main__ 中构造；multiprocessing.Manager 不能在
    fork 出的子进程里重建，因此需要广播的进程必须在使用前拿到
    fork 继承来的 ProcessSignal 实例）
· 声明为类属性、按类共享         → SignalDescriptor

回调约定
--------
所有回调签名统一为 ``cb(sender, **kwargs)``。

生命周期
--------
· Signal            —— 无资源，无需 teardown。
· ProcessSignal     —— 内部持有一个 daemon 监听线程；随进程退出而终止。
                       如需提前停止，调用 ``.close()``。
· SignalDescriptor  —— 无状态，随所属类存在。

跨进程 sender 降级
------------------
跨进程广播时 sender 无法 pickle，会被降级为 ``repr(sender)`` 字符串。
订阅方如果需要稳定标识，请让 sender 实现 ``__repr__``，
或在 kwargs 里另传一个稳定字段（例如 ``sender_id=...``）。

最小示例
--------
::

    sig = Signal("scan")
    sig.connect(lambda sender, **kw: print(sender, kw))
    sig.send(self, path="/tmp")
    # -> <self> {'path': '/tmp'}
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

_logger = logging.getLogger(__name__)

__all__ = [
    "Signal",
    "ProcessSignal",
    "SignalDescriptor",
    "get_manager",
]

# --------------------------------------------------------------------------- #
# Manager（ProcessSignal 依赖）
# --------------------------------------------------------------------------- #
_manager: Optional[Any] = None
_manager_lock = threading.Lock()


def get_manager() -> Any:
    """获取全局 Manager 单例。

    必须在主进程的 ``__main__`` 中首次调用。子进程里的
    ``multiprocessing.Manager()`` 会新建一份独立的队列，导致跨进程
    广播失效。
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = multiprocessing.Manager()
    return _manager


# --------------------------------------------------------------------------- #
# 跨进程消息体
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _WireMessage:
    """跨进程队列中传递的消息。

    ``sender_repr`` 是 sender 的 repr —— 跨进程无法传递对象引用。
    """
    sender_id: int
    sender_repr: str
    kwargs: dict


# --------------------------------------------------------------------------- #
# 线程安全信号
# --------------------------------------------------------------------------- #
class Signal:
    """进程内线程安全信号。

    回调签名：``cb(sender, **kwargs)``。

    示例::

        sig = Signal("scan")
        sig.connect(lambda sender, **kw: print(sender, kw))
        sig.send(self, path="/tmp")   # -> <self> {'path': '/tmp'}
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._lock = threading.RLock()
        self._callbacks: list[Callable[..., Any]] = []

    def connect(self, callback: Callable[..., Any]) -> None:
        """注册回调；重复注册同一 callback 只生效一次。"""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def disconnect(self, callback: Callable[..., Any]) -> None:
        """取消注册回调；未注册过的 callback 静默忽略。"""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    # noinspection broad-exception
    def send(self, sender: Any, **kwargs: Any) -> None:
        """同步触发所有回调。

        单个回调抛出的异常会被记录，不阻断后续回调。
        """
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(sender, **kwargs)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "[Signal %s] 回调 %r 出错", self.name, cb,
                )

    # 与 Qt 术语对齐的别名
    emit = send


# --------------------------------------------------------------------------- #
# 跨进程信号
# --------------------------------------------------------------------------- #
# noinspection broad-exception
class ProcessSignal:
    """跨进程 + 跨线程安全的信号。

    - 同进程内立即同步调用本地回调（与 :class:`Signal` 语义一致）；
    - 通过 ``multiprocessing.Manager().Queue()`` 广播到其他进程；
    - 其他进程的消息由一个 daemon 监听线程拉取后触发本地回调。

    **前置条件**：必须在主进程的 ``__main__`` 中构造。子进程需要广播
    时，应通过 fork 继承父进程创建的实例，而不是在子进程里重新构造。

    **sender 降级**：跨进程时 sender 无法 pickle，会退化成
    ``repr(sender)`` 字符串。订阅方如果需要稳定标识，请让 sender
    实现 ``__repr__``，或在 kwargs 里另传稳定字段。

    示例::

        # 主进程
        if __name__ == "__main__":
            sig = ProcessSignal("scan")
            sig.connect(lambda sender, **kw: print(sender, kw))
            # 子进程：广播回主进程
            sig.send("worker-1", path="/tmp")
    """

    def __init__(self, name: str = "") -> None:
        # 前置条件校验：不在主进程里构造会得到分裂的 Manager
        proc = multiprocessing.current_process()
        if proc.name != "MainProcess":
            raise RuntimeError(
                "ProcessSignal 必须在主进程 __main__ 中构造；"
                "multiprocessing.Manager 不能在 fork 出的子进程里重建。"
                f"当前进程: {proc.name!r}"
            )

        self.name = name
        self._local_signal = Signal(name)
        self._process_id = os.getpid()
        self._queue = get_manager().Queue()
        self._listener_started = False
        self._closed = False
        self._lock = threading.RLock()

    # -------------------------------------------------------------- #
    # 监听
    # -------------------------------------------------------------- #
    def _ensure_listener(self) -> None:
        if self._listener_started or self._closed:
            return
        with self._lock:
            if self._listener_started or self._closed:
                return
            self._listener_started = True
            threading.Thread(
                target=self._listen,
                name=f"ProcessSignal-{self.name}",
                daemon=True,
            ).start()

    def _listen(self) -> None:
        """从共享队列拉取消息并触发本地回调。

        靠 daemon 线程随进程退出；显式停止请调 :meth:`close`。
        """
        while not self._closed:
            try:
                msg = self._queue.get()
            except (EOFError, OSError):
                break
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "[ProcessSignal %s] 拉取消息失败", self.name,
                )
                continue

            if msg is None:  # close() 发送的哨兵
                break

            if not isinstance(msg, _WireMessage):
                _logger.warning(
                    "[ProcessSignal %s] 收到未知消息: %r",
                    self.name, msg,
                )
                continue

            # 只处理其他进程的消息；本进程的广播已在 send() 中同步执行
            if msg.sender_id != self._process_id:
                self._local_signal.send(msg.sender_repr, **msg.kwargs)

    def close(self) -> None:
        """显式停止监听线程。幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put(None)  # 唤醒阻塞在 get() 上的监听线程
        except Exception:  # noqa: BLE001
            _logger.debug(
                "[ProcessSignal %s] 发送终止哨兵失败", self.name,
            )

    # -------------------------------------------------------------- #
    # 订阅 / 触发
    # -------------------------------------------------------------- #
    def connect(self, callback: Callable[..., Any]) -> None:
        """注册回调；首次注册时自动启动监听线程。"""
        self._local_signal.connect(callback)
        self._ensure_listener()

    def disconnect(self, callback: Callable[..., Any]) -> None:
        self._local_signal.disconnect(callback)

    def send(self, sender: Any, **kwargs: Any) -> None:
        """触发信号：先本地同步调用，再广播到其他进程。

        kwargs 必须可 pickle；不可 pickle 时本地回调仍会执行，
        但跨进程广播会被跳过并记录日志。
        """
        # 1. 本地立即触发
        self._local_signal.send(sender, **kwargs)

        # 2. 广播到其他进程
        try:
            pickle.dumps(kwargs)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "[ProcessSignal %s] kwargs 不可 pickle，跳过跨进程广播",
                self.name,
            )
            return

        try:
            self._queue.put(_WireMessage(
                sender_id=self._process_id,
                sender_repr=repr(sender),
                kwargs=kwargs,
            ))
        except Exception:  # noqa: BLE001
            _logger.exception("[ProcessSignal %s] 广播失败", self.name)

    # 与 Qt 术语对齐的别名
    emit = send

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.send(*args, **kwargs)


# --------------------------------------------------------------------------- #
# 类属性声明
# --------------------------------------------------------------------------- #
# noinspection protected-member
class SignalDescriptor:
    """信号描述符：在类里声明信号，按拥有者类共享实例。

    ``cross_process=False`` → :class:`Signal`；
    ``cross_process=True``  → :class:`ProcessSignal`。

    示例::

        class Service:
            scan_finished = SignalDescriptor()
            source_added  = SignalDescriptor(cross_process=True)

        Service.scan_finished.connect(...)   # 所有实例共享
        Service.scan_finished.send(svc, path="/tmp")
    """

    def __init__(self, name: str = "", *, cross_process: bool = False) -> None:
        self.name = name
        self.cross_process = cross_process
        self._signals: dict[type, Any] = {}
        self._lock = threading.RLock()

    def __set_name__(self, owner: type, name: str) -> None:
        # 省略 name 时默认取类属性名
        if not self.name:
            self.name = name

    def __get__(self, instance: Any, owner: type) -> Any:
        if owner not in self._signals:
            with self._lock:
                if owner not in self._signals:
                    if self.cross_process:
                        self._signals[owner] = ProcessSignal(self.name)
                    else:
                        self._signals[owner] = Signal(self.name)
        signal = self._signals[owner]
        if isinstance(signal, ProcessSignal):
            signal._ensure_listener()
        return signal
