#
"""IPC 客户端实现：一次连接、同步调用、可选事件流。

连接是**惰性**建立的：``IpcClient()`` 本身不碰 socket，第一次 ``call`` /
``events`` 时才连。这样 CLI 在「应用没跑」时也能给出干净的错误，而不是
在构造函数里炸。
"""

from __future__ import annotations

import collections
import itertools
import time
from collections.abc import Iterator, Mapping
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from typing import Any

from albuswall.common.ipc import (
    DEFAULT_CALL_TIMEOUT,
    EVENT_METHOD,
    MAX_MESSAGE_BYTES,
    RemoteError,
    decode_frame,
    encode_frame,
    make_request,
)
from albuswall.utils.ipc_endpoint import Endpoint, EndpointUnavailable, endpoint

__all__ = [
    "AuthFailed",
    "CallTimeout",
    "IpcClient",
    "IpcFailure",
    "NotRunning",
    "ProtocolError",
]

#: ``events()`` 的轮询粒度：给 Ctrl+C 留出响应窗口，同时几乎不耗 CPU。
_POLL_SLICE = 0.5


class IpcFailure(Exception):
    """客户端侧失败的基类。"""


class NotRunning(IpcFailure):
    """端点不存在或没人监听：应用没在跑，或者 IPC 被配置关掉了。"""


class AuthFailed(IpcFailure):
    """token 不匹配：通常是残留 token 文件，或连到了别的用户的实例。"""


class CallTimeout(IpcFailure):
    """服务端在超时内没有回应（主循环被卡住）。"""


class ProtocolError(IpcFailure):
    """收到的帧不是本协议（版本不同 / 对端不是 AlbusWall）。"""


class IpcClient:
    """同步 IPC 客户端。

    用法::

        with IpcClient() as c:
            print(c.call("app.ping"))
    """

    def __init__(
            self,
            *,
            timeout: float = DEFAULT_CALL_TIMEOUT,
            endpoint: Endpoint | None = None,
            max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self._timeout = timeout
        self._endpoint = endpoint
        self._max_message_bytes = max_message_bytes
        self._conn: Any = None
        self._ids = itertools.count(1)
        # 调用过程中顺手收到的事件先攒着，等 events() 再交付
        self._pending: collections.deque[dict] = collections.deque()

    # ---------------- 连接 ----------------
    def _resolve(self) -> Endpoint:
        if self._endpoint is not None:
            return self._endpoint
        try:
            self._endpoint = endpoint(create=False)
        except EndpointUnavailable as exc:
            raise NotRunning(str(exc)) from exc
        return self._endpoint

    def _ensure(self) -> Any:
        if self._conn is not None:
            return self._conn
        ep = self._resolve()
        try:
            self._conn = Client(ep.address, family=ep.family, authkey=ep.authkey)
        except AuthenticationError as exc:
            raise AuthFailed(
                f"ipc authentication failed ({ep}); "
                f"the token file may be stale"
            ) from exc
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError) as exc:
            raise NotRunning(
                f"cannot connect to {ep}: {exc}; "
                f"is AlbusWall running with IPC enabled?"
            ) from exc
        except OSError as exc:
            # Windows 命名管道不存在时是 ERROR_FILE_NOT_FOUND / ERROR_PIPE_BUSY
            raise NotRunning(f"cannot connect to {ep}: {exc}") from exc
        return self._conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noinspection PyBroadException
                pass

    def __enter__(self) -> IpcClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---------------- 收发 ----------------
    def _send(self, message: Mapping[str, Any]) -> None:
        conn = self._ensure()
        try:
            conn.send_bytes(encode_frame(message))
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.close()
            raise NotRunning(f"connection lost: {exc}") from exc

    def _recv(self, timeout: float) -> dict:
        conn = self._ensure()
        try:
            available = conn.poll(timeout)
        except (OSError, EOFError) as exc:
            self.close()
            raise NotRunning(f"connection lost: {exc}") from exc
        if not available:
            raise CallTimeout(f"no response within {timeout}s")
        try:
            raw = conn.recv_bytes(maxlength=self._max_message_bytes + 16)
        except (EOFError, OSError) as exc:
            self.close()
            raise NotRunning(f"connection closed by server: {exc}") from exc
        try:
            return decode_frame(raw)
        except ValueError as exc:
            raise ProtocolError(f"malformed frame from server: {exc}") from exc

    # ---------------- 调用 ----------------
    def call(
            self,
            method: str,
            params: Mapping[str, Any] | None = None,
            *,
            timeout: float | None = None,
    ) -> Any:
        """发一次调用并等待结果。

        :raises RemoteError: 服务端执行失败（带 JSON-RPC 错误码）
        :raises NotRunning: 连不上 / 连接中途断开
        :raises AuthFailed: token 不匹配
        :raises CallTimeout: 超时
        """
        wait = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + wait
        mid = next(self._ids)
        self._send(make_request(mid, method, params))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CallTimeout(f"no response for {method!r} within {wait}s")
            message = self._recv(remaining)

            if message.get("method") == EVENT_METHOD:
                # 未订阅时也可能收到（服务端主动推），先缓存给 events()
                payload = message.get("params")
                if isinstance(payload, Mapping):
                    self._pending.append(dict(payload))
                continue

            if message.get("id") != mid:
                # 不是本次调用的响应：协议上没有并发调用，丢弃并继续等
                continue

            error = message.get("error")
            if error is not None:
                if not isinstance(error, Mapping):
                    raise ProtocolError(f"malformed error object: {error!r}")
                raise RemoteError.from_object(error)
            return message.get("result")

    # ---------------- 事件 ----------------
    def subscribe(self, *topics: str) -> list[str]:
        """订阅事件；不传 topic 表示全订阅。"""
        result = self.call("event.subscribe", {"topics": list(topics)})
        if isinstance(result, Mapping):
            raw = result.get("subscribed")
            if isinstance(raw, (list, tuple)):
                return [str(t) for t in raw]
        return []

    def events(self, timeout: float | None = None) -> Iterator[dict]:
        """持续产出事件（``params`` dict：``{"topic": ..., "data": ...}``）。

        ``timeout`` 为 ``None`` 时一直等；否则总时长超时后结束迭代。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._pending:
                yield self._pending.popleft()
                continue
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                wait = min(_POLL_SLICE, remaining)
            else:
                wait = _POLL_SLICE
            try:
                available = self._ensure().poll(wait)
            except (OSError, EOFError) as exc:
                self.close()
                raise NotRunning(f"connection lost: {exc}") from exc
            if not available:
                continue
            message = self._recv(0.0)
            if message.get("method") != EVENT_METHOD:
                continue
            payload = message.get("params")
            if isinstance(payload, Mapping):
                yield dict(payload)
