#
"""ipc/manager.py —— IPC 容器包装层 + 自注册钩子。"""
from __future__ import annotations

import threading

from albuswall.core import Container, Application

from ._client import IpcClient
from ._service import EndpointService
from ._probe import probe_instance
from ._errors import InstanceAlreadyRunning

from albuswall.log import getLogger

logger = getLogger(__package__)


class IpcManager:
    """IPC 端点生命周期对象（容器单例）。"""

    def __init__(self, endpoint: str | None = None,
                 authkey: bytes | None = None,
                 meta: dict | None = None,
                 slow_call_seconds: float = 0.2):
        self._svc = EndpointService(endpoint=endpoint,
                                    authkey=authkey,
                                    meta=meta)
        self._lock = threading.Lock()
        self._started = False
        self._slow_call_seconds = slow_call_seconds

    # ---- 生命周期 ----

    def start(self) -> None:
        """bind + start。已有实例存活时抛 InstanceAlreadyRunning。幂等。"""
        with self._lock:
            if self._started:
                return
            self._svc.bind()
            self._svc.start()
            self._started = True

    def stop(self) -> None:
        """释放端点。幂等，可安全挂在 on_exit。"""
        with self._lock:
            if not self._started:
                return
            self._svc.stop()
            self._started = False

    # noinspection broad-exception
    def wake_up_or_quit(self, exit_code: int = 0) -> None:
        """已有实例存活时的统一处理：尝试唤醒，然后抛 SystemExit。"""
        try:
            c = self._svc_client(0.5)
            c.slow_call_seconds = float("inf")  # 唤醒不是用户请求，不报慢
            with c:
                c.ping()
            logger.info("Woke up existing IPC instance.")
        except Exception:
            # 唤醒失败也不影响退出
            logger.debug("Wake-up existing instance failed.")
        raise SystemExit(exit_code)

    # ---- 业务挂接 ----

    def set_handler(self, fn) -> None:
        self._svc.set_handler(fn)

    def clear_handler(self) -> None:
        self._svc.clear_handler()

    @property
    def handler_enabled(self) -> bool:
        return self._svc.handler_enabled

    # ---- 便捷方法 ----

    def probe(self, timeout: float = 1.5) -> dict | None:
        return probe_instance(self._svc.endpoint, self._svc.authkey, timeout)

    def _svc_client(self, timeout: float) -> IpcClient:
        c = IpcClient(self._svc.endpoint, self._svc.authkey, timeout)
        c.slow_call_seconds = self._slow_call_seconds
        return c

    def meta_snapshot(self) -> dict:
        return self._svc.meta_snapshot()

    @property
    def endpoint(self) -> str:
        return self._svc.endpoint

    @property
    def authkey(self) -> bytes:
        return self._svc.authkey


def register_ipc(container: Container,
                 *,
                 endpoint: str | None = None,
                 meta: dict | None = None,
                 slow_call_seconds: float = 0.2) -> IpcManager:
    """创建 IpcManager 并挂到容器 + Application 生命周期钩子。

    register 阶段：只装配，不启动。
    启动时机：Application.on_boot（`_boot()` 完成、exec 之前）。
    停止时机：Application.on_exit。
    """
    # NOTE 急切创建貌似没问题，毕竟单实例检查需要这个
    manager = IpcManager(endpoint=endpoint, meta=meta,
                         slow_call_seconds=slow_call_seconds)
    container.register("ipc", manager, returns=IpcManager)
    # alias
    container.register(
        "ipc_service",
        lambda: container.require("ipc"),
        returns=IpcManager
    )

    # @Application.on_init
    def _setup() -> None:
        try:
            manager.start()
        except InstanceAlreadyRunning:
            logger.info("Another instance owns the endpoint; exiting.")
            manager.wake_up_or_quit(0)  # 抛 SystemExit

    Application.on_init(_setup)
    Application.on_exit(manager.stop)
    return manager
