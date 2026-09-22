#
""""""

import threading
import multiprocessing
import pickle
import os

# 全局 Manager，需在主进程中初始化（见 __main__）
_manager = None


def get_manager():
    global _manager
    if _manager is None:
        _manager = multiprocessing.Manager()
    return _manager


class Signal:
    """线程安全的信号"""

    def __init__(self, name: str=""):
        self.name = name
        self._lock = threading.RLock()
        self._callbacks = []

    def connect(self, callback):
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def disconnect(self, callback):
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def send(self, sender, **kwargs):
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(sender, **kwargs)
            except Exception as e:
                print(f"[Signal] 回调 {cb} 出错: {e}")

    # alias
    emit = send


class ProcessSignal:
    """跨进程安全的信号，同时线程安全"""

    def __init__(self, name=None):
        self.name = name
        self._local_signal = Signal()  # 本地回调管理
        self._process_id = os.getpid()
        self._queue = None
        self._listener_started = False
        self._lock = threading.RLock()
        self._init_queue()

    def _init_queue(self):
        manager = get_manager()
        self._queue = manager.Queue()

    def _start_listener(self):
        if self._listener_started:
            return
        with self._lock:
            if self._listener_started:
                return
            self._listener_started = True
            t = threading.Thread(target=self._listen, daemon=True)
            t.start()

    def _listen(self):
        """监听共享队列，收到其他进程的消息后触发本地回调"""
        while True:
            try:
                msg = self._queue.get()
                if msg is None:  # 终止信号
                    break
                sender_id, sender_repr, kwargs = msg
                if sender_id != self._process_id:
                    # 其他进程发来的消息，调用本地回调
                    self._local_signal.send(sender_repr, **kwargs)
            except (EOFError, OSError):
                break
            except Exception as e:
                print(f"[ProcessSignal] 监听出错: {e}")

    def connect(self, callback):
        self._local_signal.connect(callback)
        self._start_listener()

    def disconnect(self, callback):
        self._local_signal.disconnect(callback)

    def send(self, sender, **kwargs):
        # 1. 立即调用本地回调
        self._local_signal.send(sender, **kwargs)
        # 2. 广播到其他进程
        if self._queue:
            try:
                sender_repr = repr(sender)  # 跨进程不能传递对象引用，用字符串表示
                pickle.dumps(kwargs)  # 确保参数可序列化
                self._queue.put((self._process_id, sender_repr, kwargs))
            except Exception as e:
                print(f"[ProcessSignal] 广播失败: {e}")

    def __call__(self, *args, **kwargs):
        self.send(*args, **kwargs)

    # alias
    emit = send


class SignalDescriptor:
    """
    信号描述符，用于在类中声明信号。
    cross_process=False 时返回线程安全的 Signal；
    cross_process=True  时返回 ProcessSignal（同时线程安全+进程安全）。
    """

    def __init__(self, name=None, cross_process=False):
        self.name = name
        self.cross_process = cross_process
        self._signals = {}  # 按拥有者类缓存信号对象
        self._lock = threading.RLock()

    # noinspection PyProtectedMember
    def __get__(self, instance, owner):
        if owner not in self._signals:
            with self._lock:
                if owner not in self._signals:
                    if self.cross_process:
                        self._signals[owner] = ProcessSignal(self.name)
                    else:
                        self._signals[owner] = Signal()
        signal = self._signals[owner]
        # 如果是 ProcessSignal，确保监听线程已启动
        if isinstance(signal, ProcessSignal):
            signal._start_listener()
        return signal
