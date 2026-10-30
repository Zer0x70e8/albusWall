#
"""端点服务：单实例锁 + 连接处理 + ping 快速路径。"""
from __future__ import annotations

import os
import sys
import time
import threading
from multiprocessing.connection import Listener

from ._constants import PROTOCOL_VERSION
from ._endpoints import get_endpoint, get_authkey
from ._errors import IpcError, InstanceAlreadyRunning
from ._protocol import recv, safe_send
from ._probe import probe_instance

from albuswall.log import getLogger

logger = getLogger(__package__)


# noinspection broad-exception
class EndpointService:
    """端点服务：bind 成功即单实例锁。

    ping 由本类直接响应，不依赖 handler。
    非 ping 请求原样交给 handler；未设置 handler 时返回 no_handler。
    """

    def __init__(self, endpoint: str | None = None,
                 authkey: bytes | None = None,
                 meta: dict | None = None):
        self.endpoint = endpoint or get_endpoint()
        self.authkey = authkey or get_authkey(create=True)
        if not self.authkey:
            raise IpcError("authkey unavailable")
        self._meta = dict(meta or {})
        self._started_at = time.time()
        self._listener: Listener | None = None
        self._running = False
        self._accept_thread: threading.Thread | None = None
        self._handler = None
        self._handler_lock = threading.Lock()

    # ---- 生命周期 ----

    def bind(self) -> None:
        """绑定端点。失败抛 InstanceAlreadyRunning 或 IpcError。"""
        try:
            self._listener = Listener(self.endpoint, authkey=self.authkey)
            logger.info("IPC endpoint bound: %s", self.endpoint)
        except OSError as e:
            if sys.platform == "win32":
                raise InstanceAlreadyRunning(self.endpoint) from e
            self._listener = self._bind_after_zombie_cleanup(e)

        if sys.platform != "win32":
            try:
                os.chmod(self.endpoint, 0o600)
            except OSError:
                pass

    def _bind_after_zombie_cleanup(self, original: OSError) -> Listener:
        if probe_instance(self.endpoint, self.authkey, timeout=0.5):
            logger.info("IPC endpoint %s held by live instance.", self.endpoint)
            raise InstanceAlreadyRunning(self.endpoint) from original
        time.sleep(0.2)
        if probe_instance(self.endpoint, self.authkey, timeout=0.5):
            logger.info("IPC endpoint %s held by live instance (race).", self.endpoint)
            raise InstanceAlreadyRunning(self.endpoint) from original
        logger.warning("cleaning up stale socket: %s", self.endpoint)
        try:
            os.unlink(self.endpoint)
        except FileNotFoundError:
            pass
        try:
            listener = Listener(self.endpoint, authkey=self.authkey)
            logger.info("IPC endpoint re-bound after cleanup: %s", self.endpoint)
            return listener
        except OSError as e:
            raise InstanceAlreadyRunning(self.endpoint) from e

    def start(self) -> None:
        """bind 之后调用，启动 accept 线程。ping 即刻可用。"""
        if self._listener is None:
            raise IpcError("must bind() before start()")
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="ipc-accept", daemon=True,
        )
        # noinspection unresolved-references
        self._accept_thread.start()
        logger.info("IPC service started (accept thread up).")

    def stop(self) -> None:
        self._running = False
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
        if sys.platform != "win32":
            try:
                os.unlink(self.endpoint)
            except FileNotFoundError:
                pass
        logger.info("IPC service stopped.")

    # ---- 业务挂接点 ----

    def set_handler(self, fn) -> None:
        """fn(req: dict) -> Any。

        req 已过协议版本校验，未做任何业务语义解析——method/params 的
        含义完全由业务层决定。ping 由 IPC 层直接响应，不进 handler。
        未设置 handler 时，非 ping 请求返回 no_handler。
        调用方负责线程语义（本类在连接处理线程里调用它）。
        """
        with self._handler_lock:
            self._handler = fn

    def clear_handler(self) -> None:
        with self._handler_lock:
            self._handler = None

    @property
    def handler_enabled(self) -> bool:
        return self._handler is not None

    # ---- 元信息 ----

    def meta_snapshot(self) -> dict:
        return {
            "pid": os.getpid(),
            "endpoint": self.endpoint,
            "started_at": self._started_at,
            "uptime": time.time() - self._started_at,
            "protocol_version": PROTOCOL_VERSION,
            "handler_enabled": self.handler_enabled,
            **self._meta,
        }

    # ---- 内部 ----

    def _accept_loop(self) -> None:
        while self._running:
            try:
                # noinspection unresolved-references
                conn = self._listener.accept()
            except (OSError, EOFError):
                if not self._running:
                    break
                continue
            logger.trace("IPC connection accepted.")
            threading.Thread(
                target=self._handle, args=(conn,),
                name="ipc-conn", daemon=True,
            ).start()

    def _handle(self, conn) -> None:
        with conn:
            try:
                req = recv(conn)
            except Exception as e:
                logger.debug("bad frame: %s", e, exc_info=True)
                return

            if req.get("v") != PROTOCOL_VERSION:
                safe_send(conn, {"ok": False, "code": "bad_version",
                                 "error": f"unsupported: {req.get('v')}"})
                return

            logger.trace("IPC request: keys=%r", list(req.keys()))

            # —— ping 快速路径：IPC 自身健康检查，不经 handler ——
            if req.get("method") == "ping":
                safe_send(conn, {"ok": True, "result": self.meta_snapshot()})
                return

            handler = self._handler
            if handler is None:
                safe_send(conn, {"ok": False, "code": "no_handler",
                                 "error": "no handler installed"})
                return

            try:
                # noinspection calling-non-callable
                result = handler(req)
                safe_send(conn, {"ok": True, "result": result})
            except IpcError as e:
                payload = {
                    "ok": False,
                    "code": getattr(e, "code", "error"),
                    "error": str(e),
                }
                # 「建议退出码」由抛出方直接写在异常属性上：业务侧
                # ``ipc_request_service`` 构造 ``IpcRemoteError(..., exit_code=)``
                # 时带入（值来自 ``DispatchError.exit_code``）。纯 IPC 层错误
                # 没有该值 → None，字段不出现，客户端用自己的默认值。
                exit_code = getattr(e, "exit_code", None)
                if exit_code is not None:
                    payload["exit_code"] = int(exit_code)
                safe_send(conn, payload)
            except Exception as e:
                logger.exception("handler failed")
                safe_send(conn, {"ok": False, "code": "internal_error",
                                 "error": str(e)})
