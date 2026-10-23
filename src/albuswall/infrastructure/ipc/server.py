#
"""IPC 服务端：把本进程变成「可被别的进程调用」的宿主。

职责边界
--------
本模块**只做传输**：接受连接、读写帧、把请求交给 ``Dispatcher``、
把结果编成响应发回去。它不认识任何具体命令，也不 import 服务层。

线程模型
--------
* **接受线程**（1 个）：阻塞在 ``listener.accept()``。
* **连接线程**（每连接 1 个）：阻塞在 ``recv_bytes()``，串行处理该连接
  的请求。命令本身在 ``Dispatcher`` 里被搬到主循环线程执行，所以这里
  阻塞等待是安全的——它阻塞的是连接线程，不是 UI。
* **主循环线程**：真正执行命令。

为什么不用 asyncio
------------------
``asyncio.start_unix_server`` 在 Windows 的默认 ProactorEventLoop 上直接
``NotImplementedError``，跨不了平台。``multiprocessing.connection`` 的
``Listener`` 在 POSIX 走 ``AF_UNIX``、在 Windows 走 ``AF_PIPE``，是同一套
API，因此这里用「线程 + 阻塞 accept」换取一条实现跑两个平台。

已知粗糙点
----------
``Listener.accept()`` 在做完 ``accept`` 后还会同步做 ``authkey`` 挑战应答，
所以一个连上就不说话的客户端会占住接受线程直到超时。本地信任边界内
可以接受；真要防 DoS 得换 asyncio + 每连接协程。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from multiprocessing.connection import Client, Listener
from typing import TYPE_CHECKING, Any

from albuswall.common.enums import IpcErrorCode
from albuswall.common.ipc import (
    DEFAULT_CALL_TIMEOUT,
    MAX_MESSAGE_BYTES,
    InvalidParams,
    IpcProtocolError,
    decode_frame,
    encode_frame,
    make_error,
    make_event,
    make_result,
)
from albuswall.log import getLogger
from albuswall.utils import ipc_endpoint
from albuswall.utils.ipc_endpoint import Endpoint

if TYPE_CHECKING:
    from albuswall.services.command import Dispatcher

__all__ = ["IpcServer"]

_logger = getLogger(__name__)

#: 接受线程在停服时被唤醒的最长等待（秒）；超时只记日志，不阻塞退出。
_ACCEPT_JOIN_TIMEOUT = 3.0

#: 探测 / 唤醒用的一次性连接超时（秒）。
_PROBE_TIMEOUT = 1.0

#: 服务端主动推送用的订阅通配符。
_TOPIC_ALL = "*"

#: accept 连续失败的容忍次数；超过就放弃接受线程（避免占着线程空转）。
#: 取值偏大是因为「坏 token 的客户端」也会计入这里——容忍窗口太小，
#: 同机器上任何一个手滑的客户端都能让应用从 CLI 侧失联。
_MAX_ACCEPT_FAILURES = 512


class _Client:
    """一条已认证连接：发送加锁，记录事件订阅。"""

    __slots__ = ("_send_lock", "conn", "peer", "topics")

    def __init__(self, conn: Any, peer: str) -> None:
        self.conn = conn
        # 事件广播来自任意工作线程，与响应可能并发——必须串行化写入
        self._send_lock = threading.Lock()
        self.topics: set[str] | None = None  # None = 未订阅
        self.peer = peer

    def wants(self, topic: str) -> bool:
        if self.topics is None:
            return False
        return _TOPIC_ALL in self.topics or topic in self.topics

    def send(self, message: Mapping[str, Any]) -> bool:
        try:
            frame = encode_frame(message)
        except Exception:  # noinspection PyBroadException
            _logger.exception("cannot encode outbound frame")
            return False
        with self._send_lock:
            try:
                self.conn.send_bytes(frame)
                return True
            except Exception:  # noinspection PyBroadException
                return False

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noinspection PyBroadException
            pass


class IpcServer:
    """监听端点并把请求转发给 ``Dispatcher``。"""

    def __init__(
            self,
            dispatcher: Dispatcher,
            *,
            endpoint: Endpoint | None = None,
            call_timeout: float = DEFAULT_CALL_TIMEOUT,
            max_message_bytes: int = MAX_MESSAGE_BYTES,
            max_connections: int = 8,
            name: str = "ipc",
    ) -> None:
        self._dispatcher = dispatcher
        self._endpoint = endpoint
        self._call_timeout = call_timeout
        self._max_message_bytes = max_message_bytes
        self._max_connections = max_connections
        self.name = name

        self._listener: Listener | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._accept_thread: threading.Thread | None = None

        # 观测计数（不追求精确，够定位问题即可）
        self._accepted = 0
        self._rejected = 0
        self._handled = 0
        self._errors = 0
        self._last_error: str | None = None

    # ================================================================
    # 生命周期
    # ================================================================
    @property
    def running(self) -> bool:
        return self._listener is not None

    @property
    def endpoint_address(self) -> str | None:
        return self._endpoint.address if self._endpoint else None

    def start(self) -> bool:
        """开始监听。

        :returns: ``True`` 表示本次启动成功；``False`` 表示已经有实例
            占着端点（单实例语义，不是错误）。
        :raises Exception: 其它启动失败（权限、路径过长等）原样抛出。
        """
        if self._listener is not None:
            return True

        endpoint = self._endpoint or ipc_endpoint.endpoint(create=True)
        self._endpoint = endpoint

        if self._another_instance_alive(endpoint):
            _logger.info(
                "IPC endpoint %s is already served by another instance", endpoint
            )
            return False

        self._prepare_address(endpoint)

        self._listener = Listener(
            endpoint.address,
            family=endpoint.family,
            backlog=max(4, self._max_connections),
            authkey=endpoint.authkey,
        )
        self._tighten_socket_permissions(endpoint)

        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"{self.name}-accept",
            daemon=True,
        )
        self._accept_thread.start()
        _logger.info("IPC server listening on %s", endpoint)
        return True

    def stop(self, timeout: float = _ACCEPT_JOIN_TIMEOUT) -> None:
        """停止监听：唤醒接受线程 → 断开所有连接 → 释放端点。

        幂等，可在 ``on_final`` 里无条件调用。
        """
        if self._listener is None:
            return
        self._stop.set()

        # ① 自连一次唤醒阻塞在 accept() 的接受线程。
        #    在 Linux 上从别的线程 close() 监听 socket 并不会唤醒 accept，
        #    所以必须用「连一下」而不是「关掉」。
        self._wake_accept_loop()

        thread = self._accept_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                _logger.warning("IPC accept thread did not exit in %.1fs", timeout)

        # ② 断开所有连接（会解除连接线程的 recv 阻塞）
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()

        # ③ 释放端点。POSIX 上 Listener.close() 会顺手 unlink socket 文件。
        listener = self._listener
        self._listener = None
        try:
            listener.close()
        except Exception:  # noinspection PyBroadException
            _logger.exception("failed to close IPC listener")

        self._accept_thread = None
        _logger.info("IPC server stopped on %s", self._endpoint)

    # ================================================================
    # 事件推送
    # ================================================================
    def broadcast(self, topic: str, data: Any = None) -> int:
        """向所有已订阅该 topic 的连接推送一条事件，返回送达数。

        线程安全：可以挂在任意服务的 ``Signal`` 上（工作线程 emit 也没问题）。
        """
        if not self._clients:
            return 0
        message = make_event(topic, data)
        with self._lock:
            clients = list(self._clients)
        return sum(1 for c in clients if c.wants(topic) and c.send(message))

    def stats(self) -> dict[str, Any]:
        """自省快照（``ipc.stats`` / ``albusctl status``）。

        这是**唯一**在服务器线程本地处理的方法：它不碰主循环、不碰任何
        服务，所以主循环被阻塞时依然能回答「我现在什么状态」。
        """
        with self._lock:
            clients = len(self._clients)
        dispatcher = self._dispatcher
        return {
            "running": self.running,
            "address": self.endpoint_address,
            "clients": clients,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "handled": self._handled,
            "errors": self._errors,
            "last_error": self._last_error,
            # 分发器是否已绑定主循环：false 时命令会以 UNAVAILABLE 失败，
            # 但本方法与订阅命令仍然可用。
            "dispatcher_bound": bool(getattr(dispatcher, "bound", False)),
            # 构造时生效的上限，便于排查「为什么大响应被拒 / 为什么超时」。
            "limits": {
                "call_timeout": self._call_timeout,
                "max_message_bytes": self._max_message_bytes,
                "max_connections": self._max_connections,
            },
        }

    # ================================================================
    # 启动辅助
    # ================================================================
    def _another_instance_alive(self, endpoint: Endpoint) -> bool:
        """探测端点是否已经被别的进程服务。

        只把「连上了」当作有人在跑；``ConnectionRefusedError`` /
        ``FileNotFoundError`` 都表示文件在、没人听，属于陈旧残留。
        """
        return self._connect_probe(endpoint, _PROBE_TIMEOUT)

    @staticmethod
    def _connect_probe(endpoint: Endpoint, timeout: float) -> bool:
        """带超时的「连一下」。

        **为什么必须有超时**：``Client()`` 内部会做 authkey 挑战应答，
        而对端只有在 ``accept()`` 里才会应答。如果监听端还在监听、但接受
        线程已经退出（我们不希望发生，但重启竞态里可能出现），握手就会
        一直等下去。把连接放进守护线程并只等 ``timeout``，可以让 stop()
        和 start() 永远不被它拖住。
        """
        result = {"ok": False}
        done = threading.Event()

        def _try() -> None:
            conn = None
            try:
                conn = Client(
                    endpoint.address,
                    family=endpoint.family,
                    authkey=endpoint.authkey,
                )
                result["ok"] = True
            except Exception:  # noinspection PyBroadException
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noinspection PyBroadException
                        pass
                done.set()

        thread = threading.Thread(
            target=_try, name="ipc-probe", daemon=True,
        )
        thread.start()
        return done.wait(timeout) and result["ok"]

    def _prepare_address(self, endpoint: Endpoint) -> None:
        """清掉陈旧的 socket 文件。

        ``multiprocessing.connection`` 的 ``SocketListener`` 只在 ``close()``
        时 unlink，bind 之前不管；残留文件会让 bind 直接 ``EADDRINUSE``。
        """
        if endpoint.family != "AF_UNIX":
            return
        try:
            os.unlink(endpoint.address)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.debug("cannot unlink stale socket %s: %s", endpoint.address, exc)

    @staticmethod
    def _tighten_socket_permissions(endpoint: Endpoint) -> None:
        """POSIX：socket 文件收紧到 0600。

        ``bind()`` 建出来的文件权限受 umask 影响，通常是 0644——同机器上
        任何用户都能连。显式 chmod 是这层安全的唯一保障（没有 SO_PEERCRED）。
        """
        if endpoint.family != "AF_UNIX":
            return
        try:
            os.chmod(endpoint.address, 0o600)
        except OSError as exc:
            _logger.warning(
                "cannot chmod IPC socket %s to 0600: %s", endpoint.address, exc
            )

    def _wake_accept_loop(self) -> None:
        """自连一次，唤醒阻塞在 ``accept()`` 的接受线程。

        在 Linux 上从别的线程 ``close()`` 监听 socket **不会**唤醒
        ``accept()``（实测：线程会一直挂在里面），所以只能靠「来一个连接」。
        结果不重要——只求把 ``accept()`` 叫醒，让它看见 ``_stop``。
        """
        endpoint = self._endpoint
        if endpoint is None:
            return
        self._connect_probe(endpoint, _PROBE_TIMEOUT)

    # ================================================================
    # 接受线程
    # ================================================================
    def _accept_loop(self) -> None:
        listener = self._listener
        failures = 0
        while listener is not None and not self._stop.is_set():
            try:
                conn = listener.accept()
            except Exception as exc:
                # ``accept()`` 内部会同步跑 authkey 挑战应答，所以这里可能抛的
                # 不只是 OSError：token 不对是 ``AuthenticationError``，
                # 客户端连上就断是 ``EOFError`` / ``BrokenPipeError``。
                # 全都是**单连接**级别的失败，接受线程必须活下来。
                if self._stop.is_set():
                    break
                self._note_error(f"accept failed: {exc!r}")
                failures += 1
                _logger.debug("IPC accept attempt failed (%d): %r", failures, exc)
                if failures >= _MAX_ACCEPT_FAILURES:
                    _logger.error(
                        "IPC accept loop giving up after %d consecutive failures",
                        failures,
                    )
                    break
                # 冷却一下：坏客户端会立刻重连，空转只烧 CPU
                time.sleep(0.02)
                continue
            failures = 0
            if self._stop.is_set():
                conn.close()
                break
            self._accept(conn)

    def _accept(self, conn: Any) -> None:
        with self._lock:
            too_many = len(self._clients) >= self._max_connections
            if not too_many:
                self._accepted += 1
        if too_many:
            self._rejected += 1
            _logger.warning(
                "rejecting IPC connection: %d already open", self._max_connections
            )
            try:
                conn.close()
            except Exception:  # noinspection PyBroadException
                pass
            return

        peer = f"conn-{self._accepted}"
        client = _Client(conn, peer)
        with self._lock:
            self._clients.append(client)
        threading.Thread(
            target=self._serve_client,
            args=(client,),
            name=f"{self.name}-{peer}",
            daemon=True,
        ).start()

    # ================================================================
    # 连接线程
    # ================================================================
    def _serve_client(self, client: _Client) -> None:
        requests = 0
        _logger.debug("ipc %s connected", client.peer)
        try:
            while not self._stop.is_set():
                try:
                    raw = client.conn.recv_bytes(
                        maxlength=self._max_message_bytes + 16,
                    )
                except EOFError:
                    break
                except OSError as exc:
                    if not self._stop.is_set():
                        _logger.debug("ipc %s recv failed: %s", client.peer, exc)
                    break
                requests += 1
                self._on_frame(client, raw)
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            client.close()
            _logger.debug(
                "ipc %s disconnected after %d request(s)", client.peer, requests
            )

    def _on_frame(self, client: _Client, raw: bytes) -> None:
        mid: Any = None
        try:
            message = decode_frame(raw)
        except ValueError as exc:
            self._note_error(f"{client.peer}: parse error: {exc}")
            client.send(make_error(
                None, IpcErrorCode.PARSE_ERROR, f"invalid frame: {exc}"
            ))
            return

        mid = message.get("id")
        method = message.get("method")
        raw_params = message.get("params")
        params: Any = {} if raw_params is None else raw_params

        if not isinstance(method, str) or not method:
            self._note_error(f"{client.peer}: invalid request: missing method")
            if mid is not None:
                client.send(make_error(
                    mid, IpcErrorCode.INVALID_REQUEST, "missing 'method'"
                ))
            return
        if not isinstance(params, Mapping):
            self._note_error(f"{client.peer}: {method}: params is not an object")
            if mid is not None:
                client.send(make_error(
                    mid, IpcErrorCode.INVALID_PARAMS, "'params' must be an object"
                ))
            return

        started = time.monotonic()
        try:
            result = self._dispatch(client, method, dict(params))
        except IpcProtocolError as exc:
            # 调用方用错了命令名 / 参数：这是**对方**的问题，值得在默认级别看见
            _logger.warning(
                "ipc %s req#%s %s rejected: [%s] %s",
                client.peer, mid, method, int(exc.code), exc.message,
            )
            if mid is not None:
                client.send(make_error(mid, exc.code, exc.message, exc.data))
            return
        except Exception as exc:  # noinspection PyBroadException
            self._note_error(f"{client.peer}: {method}: {exc!r}")
            _logger.exception("ipc %s req#%s %s raised", client.peer, mid, method)
            if mid is not None:
                client.send(make_error(
                    mid,
                    IpcErrorCode.INTERNAL_ERROR,
                    f"{type(exc).__name__}: {exc}",
                ))
            return

        self._handled += 1
        if mid is not None:
            client.send(make_result(mid, result))
        # 成功路径逐条记录（DEBUG）：脚本轮询时不该在 INFO 级别刷屏
        _logger.debug(
            "ipc %s req#%s %s ok in %.1f ms",
            client.peer, mid, method, (time.monotonic() - started) * 1000.0,
        )

    def _dispatch(
            self,
            client: _Client,
            method: str,
            params: dict[str, Any],
    ) -> Any:
        """本地命令 + 内置的订阅命令。"""
        if method == "event.subscribe":
            return self._subscribe(client, params)
        if method == "event.unsubscribe":
            client.topics = None
            return {"subscribed": []}
        if method == "ipc.stats":
            return self.stats()

        # 其余一律交给命令层；命令层负责「搬到主循环线程」
        return self._dispatcher.call(method, params, timeout=self._call_timeout)

    @staticmethod
    def _subscribe(client: _Client, params: Mapping[str, Any]) -> dict[str, Any]:
        topics = params.get("topics", [_TOPIC_ALL])
        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, (list, tuple)) or not all(
                isinstance(t, str) for t in topics
        ):
            raise InvalidParams("'topics' must be a string or a list of strings")
        client.topics = set(topics) or {_TOPIC_ALL}
        # noinspection PyTypeChecker
        return {"subscribed": sorted(client.topics or ())}

    def _note_error(self, message: str) -> None:
        """记录一次协议层问题。

        WARNING 而不是 DEBUG：帧坏了 / 命令名错了属于**对方**行为异常，
        默认 ``[log] level = WARNING`` 下也必须能看见——否则排查时
        只看得到一个静静不动的 ``albusctl``。
        """
        self._errors += 1
        self._last_error = message
        _logger.warning("ipc protocol error: %s", message)
