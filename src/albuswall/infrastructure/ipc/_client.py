#
"""IPC 客户端。"""

from __future__ import annotations

import time
from multiprocessing.connection import Client
from multiprocessing.context import AuthenticationError

from albuswall.log import getLogger

from ._constants import PROTOCOL_VERSION
from ._endpoints import get_endpoint, get_authkey
from ._errors import (
    IpcError,
    IpcProtocolError,
    IpcUnavailable,
    IpcTimeout,
    IpcRemoteError,
)
from ._protocol import send, recv

_CONNECT_ERRORS = (
    FileNotFoundError,
    ConnectionRefusedError,
    ConnectionResetError,
    BrokenPipeError,
    OSError,
    EOFError,
    AuthenticationError,
)

# 慢请求升 logging.WARNING 阈值；默认 200ms
_SLOW_CALL_SECONDS = 0.2

_logger = getLogger(__package__)


class IpcClient:
    slow_call_seconds: float = _SLOW_CALL_SECONDS

    def __init__(self, endpoint: str | None = None,
                 authkey: bytes | None = None,
                 timeout: float = 5.0):
        self.endpoint = endpoint or get_endpoint()
        self.authkey = authkey or get_authkey(create=False)
        self.timeout = timeout
        self._conn = None

    def connect(self) -> "IpcClient":
        if self.authkey is None:
            raise IpcUnavailable("authkey not found")
        try:
            self._conn = Client(self.endpoint, authkey=self.authkey)
        except _CONNECT_ERRORS as e:
            raise IpcUnavailable(str(e)) from e
        return self

    def call(self, method: str, params: dict | None = None,
             timeout: float | None = None):
        """便利方法：构造 {"method": ..., "params": ...} 后走 call_raw。

        method/params 只是客户端侧的常见请求形状，服务端若使用其它形状，
        请改用 call_raw 自行构造 request dict。
        """
        return self.call_raw({"method": method, "params": params or {}},
                             timeout=timeout)

    def call_raw(self, req: dict, timeout: float | None = None):
        """发送原始 request dict；除 v 外的字段由业务层解释。"""
        start = time.perf_counter()
        ok = False
        try:
            result = self._do_call(req, timeout)
            ok = True
            return result
        finally:
            elapsed = time.perf_counter() - start
            if ok and elapsed >= self.slow_call_seconds:
                _logger.warning(
                    "Slow IPC call: elapsed=%.3fs timeout=%r req_keys=%r",
                    elapsed, timeout, sorted(req.keys()),
                )

    def _do_call(self, req: dict, timeout: float | None = None):
        if self._conn is None:
            self.connect()
        effective = self.timeout if timeout is None else timeout
        req = dict(req)
        req.setdefault("v", PROTOCOL_VERSION)
        try:
            send(self._conn, req)
            if not self._conn.poll(effective):
                raise IpcTimeout(f"{req.get('method')!r} timed out after {effective}s")
        except IpcError:
            raise
        except _CONNECT_ERRORS as e:
            raise IpcUnavailable(str(e)) from e
        except Exception as e:
            raise IpcProtocolError(str(e)) from e

        resp = recv(self._conn)
        if resp.get("v") != PROTOCOL_VERSION:
            raise IpcProtocolError(
                f"protocol mismatch: server={resp.get('v')!r} "
                f"client={PROTOCOL_VERSION!r}"
            )
        if not resp.get("ok"):
            # 服务端对业务异常会带上「建议退出码」，纯 IPC 层错误没有该字段
            # → None，由调用方用自己的默认值。exit_code 是构造参数（见
            # _errors.IpcRemoteError），不再事后挂属性。
            raise IpcRemoteError(
                resp.get("error", "unknown"),
                code=resp.get("code", "remote_error"),
                exit_code=resp.get("exit_code"),
            )
        return resp.get("result")

    def ping(self, timeout: float | None = None) -> dict:
        return self.call("ping", timeout=timeout)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "IpcClient":
        return self.connect()

    def __exit__(self, *exc):
        self.close()
