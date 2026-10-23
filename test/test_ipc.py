#
"""IPC 端到端测试：HeadlessMainLoop + Dispatcher + 真实 socket。

这里刻意**不**起 Qt：``HeadlessMainLoop`` 同样实现 ``call_soon``，
因此「命令必须回到主循环线程执行」这条最关键的性质可以在无 GUI 环境下
验证 —— 跨平台 CI 也能跑。
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from albuswall.client.core import (
    AuthFailed,
    IpcClient,
    NotRunning,
    ProtocolError,
)
from albuswall.common.enums import IpcErrorCode
from albuswall.common.ipc import RemoteError
from albuswall.core.main_loop import HeadlessMainLoop
from albuswall.infrastructure.ipc.server import IpcServer
from albuswall.services.command import Dispatcher, command
from albuswall.utils.ipc_endpoint import Endpoint


# --------------------------------------------------------------------------- #
# 测试专用的命令
# --------------------------------------------------------------------------- #
@command("test.echo")
def _echo(container, *, value: object = None, boom: bool = False) -> dict:
    if boom:
        raise RuntimeError("boom")
    # 记录执行线程，供「是否回到主循环线程」断言使用
    return {"value": value, "thread": threading.current_thread().name}


@command("test.whoami")
def _whoami(container) -> dict:
    return {"thread": threading.current_thread().name}


@command("test.container")
def _container_identity(container) -> dict:
    return {"id": id(container)}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class _FakeContainer:
    """Dispatcher 只把容器透传给 handler，鸭子类型足够。"""

    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def get(self, name: str, default=None):
        return self.items.get(name, default)

    def has_instance(self, name: str) -> bool:
        return name in self.items


@pytest.fixture
def ipc_stack(tmp_path):
    """起一套 (loop, dispatcher, server)，测试结束按序拆掉。"""
    container = _FakeContainer()
    loop = HeadlessMainLoop()
    dispatcher = Dispatcher(container)  # type: ignore[arg-type]
    dispatcher.bind(loop)

    endpoint = Endpoint(
        family="AF_UNIX",
        address=str(tmp_path / "test.sock"),
        authkey=b"0123456789abcdef0123456789abcdef",
    )
    server = IpcServer(dispatcher, endpoint=endpoint, call_timeout=5.0)
    assert server.start() is True

    runner = threading.Thread(target=loop.run, name="main-loop", daemon=True)
    runner.start()

    yield {"loop": loop, "dispatcher": dispatcher, "server": server,
           "endpoint": endpoint, "container": container}

    dispatcher.close()
    server.stop(timeout=3.0)
    loop.quit(0)
    runner.join(3.0)


def _client(stack, timeout: float = 5.0) -> IpcClient:
    return IpcClient(endpoint=stack["endpoint"], timeout=timeout)


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_ping_reaches_main_loop_thread(ipc_stack):
    with _client(ipc_stack) as client:
        result = client.call("app.ping")
    assert result["ok"] is True
    assert result["pid"] == os.getpid()


def test_command_runs_on_main_loop_thread(ipc_stack):
    """最关键的性质：handler 必须在主循环线程里执行，而不是连接线程。"""
    loop_name = "main-loop"
    ipc_stack["loop"].call_soon(lambda: None)  # 确保 loop 已在运行

    with _client(ipc_stack) as client:
        result = client.call("test.whoami")

    assert result["thread"] == loop_name
    handler_thread = result["thread"]
    assert not handler_thread.startswith("ipc"), (
        f"handler ran on the IPC thread: {handler_thread}"
    )


def test_params_roundtrip(ipc_stack):
    with _client(ipc_stack) as client:
        result = client.call("test.echo", {"value": {"nested": [1, 2, 3]}})
    assert result["value"] == {"nested": [1, 2, 3]}


def test_unknown_method_raises_remote_error(ipc_stack):
    with _client(ipc_stack) as client, pytest.raises(RemoteError) as excinfo:
        client.call("no.such.command")
    assert excinfo.value.code == int(IpcErrorCode.METHOD_NOT_FOUND)


def test_invalid_params_raises_remote_error(ipc_stack):
    with _client(ipc_stack) as client, pytest.raises(RemoteError) as excinfo:
        client.call("test.echo", {"nope": 1})
    assert excinfo.value.code == int(IpcErrorCode.INVALID_PARAMS)


def test_handler_exception_becomes_internal_error(ipc_stack):
    with _client(ipc_stack) as client, pytest.raises(RemoteError) as excinfo:
        client.call("test.echo", {"boom": True})
    assert excinfo.value.code == int(IpcErrorCode.INTERNAL_ERROR)
    assert "boom" in excinfo.value.message


def test_container_is_the_registered_one(ipc_stack):
    with _client(ipc_stack) as client:
        result = client.call("test.container")
    assert result["id"] == id(ipc_stack["container"])


# --------------------------------------------------------------------------- #
# ipc.stats（服务器线程本地处理；albusctl status 的后端）
# --------------------------------------------------------------------------- #
def test_ipc_stats_reports_server_state(ipc_stack):
    """ipc.stats 不经过主循环：服务器自己就能答，且带上生效上限。"""
    with _client(ipc_stack) as client:
        result = client.call("ipc.stats")

    assert result["running"] is True
    assert result["address"] == ipc_stack["endpoint"].address
    assert result["clients"] >= 1          # 就是发起这次调用的客户端
    assert result["accepted"] >= 1
    assert result["dispatcher_bound"] is True
    assert result["limits"]["call_timeout"] == 5.0
    assert result["limits"]["max_connections"] >= 1


def test_ipc_stats_works_while_dispatcher_is_closed(ipc_stack):
    """分发器关掉（拆机中）之后，ipc.stats 仍然可答 —— 自省不依赖主线程。"""
    ipc_stack["dispatcher"].close()
    with _client(ipc_stack) as client:
        result = client.call("ipc.stats")
    assert result["dispatcher_bound"] is False
    assert result["running"] is True


def test_not_running_when_endpoint_missing(tmp_path):
    endpoint = Endpoint(
        family="AF_UNIX",
        address=str(tmp_path / "nobody.sock"),
        authkey=b"x" * 32,
    )
    with pytest.raises(NotRunning):
        IpcClient(endpoint=endpoint, timeout=2.0).call("app.ping")


def test_wrong_authkey_is_rejected(ipc_stack):
    wrong = Endpoint(
        family=ipc_stack["endpoint"].family,
        address=ipc_stack["endpoint"].address,
        authkey=b"f" * 32,
    )
    with pytest.raises(AuthFailed):
        IpcClient(endpoint=wrong, timeout=2.0).call("app.ping")


def test_second_server_reports_single_instance(ipc_stack):
    """同一端点上的第二个监听者应当让位，而不是崩掉或抢端口。"""
    other = IpcServer(
        ipc_stack["dispatcher"],
        endpoint=ipc_stack["endpoint"],
        call_timeout=5.0,
    )
    assert other.start() is False
    assert other.running is False


def test_events_are_pushed_to_subscribers(ipc_stack):
    server: IpcServer = ipc_stack["server"]
    with _client(ipc_stack) as client:
        assert client.subscribe("demo.topic") == ["demo.topic"]
        assert server.broadcast("demo.topic", {"n": 1}) == 1
        event = next(client.events(timeout=5.0))
    assert event == {"topic": "demo.topic", "data": {"n": 1}}


def test_events_are_not_sent_without_subscription(ipc_stack):
    server: IpcServer = ipc_stack["server"]
    with _client(ipc_stack) as client:
        client.call("app.ping")  # 建立连接但**不**订阅
        assert server.broadcast("demo.topic", {"n": 2}) == 0


def test_loop_stops_promptly_after_quit(ipc_stack):
    loop: HeadlessMainLoop = ipc_stack["loop"]
    time.sleep(0.05)
    loop.quit(7)
    deadline = time.monotonic() + 3.0
    while loop.code != 7 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert loop.code == 7


def test_garbage_frame_does_not_kill_the_server(ipc_stack):
    """发一坨非 JSON 的字节，服务端应当回错误并继续接受后续调用。"""
    from multiprocessing.connection import Client

    ep = ipc_stack["endpoint"]
    conn = Client(ep.address, family=ep.family, authkey=ep.authkey)
    try:
        conn.send_bytes(b"\x00not json")
        raw = conn.recv_bytes()
    finally:
        conn.close()
    assert b"error" in raw

    with _client(ipc_stack) as client:
        assert client.call("app.ping")["ok"] is True


def test_malformed_frame_client_side_is_protocol_error(ipc_stack):
    from multiprocessing.connection import Client

    ep = ipc_stack["endpoint"]
    conn = Client(ep.address, family=ep.family, authkey=ep.authkey)
    try:
        conn.send_bytes(b"[1, 2, 3]")  # 合法 JSON 但不是对象
        raw = conn.recv_bytes()
    finally:
        conn.close()
    assert b"error" in raw

    client = IpcClient(endpoint=ep, timeout=2.0)
    with pytest.raises(ProtocolError):
        # 服务端下发的畸形帧 → 客户端侧 ProtocolError
        client._conn = _FixedFrameConnection(b'{"id": 1, "result": ')
        client.call("app.ping")


class _FixedFrameConnection:
    """喂给客户端一帧固定的坏数据，验证客户端的解码失败路径。"""

    def __init__(self, frame: bytes) -> None:
        self._frame = frame
        self.closed = False

    def send_bytes(self, _data: bytes) -> None:
        return None

    def poll(self, _timeout: float = 0.0) -> bool:
        return True

    def recv_bytes(self, maxlength: int = 0) -> bytes:
        return self._frame

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# CLI 参数归一化（不需要 socket）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # 未知的第一个词 → 当作远端命令，补 call
        (["task.stats"], ["call", "task.stats"]),
        (["source.list", "{}"], ["call", "source.list", "{}"]),
        # 已知子命令原样保留
        (["stats"], ["stats"]),
        (["call", "task.stats"], ["call", "task.stats"]),
        # 全局选项在命令之前
        (["--json", "task.stats"], ["--json", "call", "task.stats"]),
        (["--timeout", "5", "task.stats"], ["--timeout", "5", "call", "task.stats"]),
        (["--timeout=5", "task.stats"], ["--timeout=5", "call", "task.stats"]),
        # 没有命令 / 只有选项 → 不插入
        ([], []),
        (["--json"], ["--json"]),
        (["-h"], ["-h"]),
    ],
)
def test_normalize_argv(argv, expected):
    from albuswall.client.cli import _normalize_argv

    assert _normalize_argv(argv) == expected


def test_cli_forwards_bare_method_name(ipc_stack, monkeypatch, capsys):
    """端到端：`albusctl test.whoami` 应当等价于 `albusctl call test.whoami`。"""
    from albuswall.client.cli import main
    from albuswall.client.core import IpcClient

    endpoint: Endpoint = ipc_stack["endpoint"]
    # 让 CLI 用测试端点而不是真实 XDG 路径
    monkeypatch.setattr(
        IpcClient, "_resolve", lambda self: endpoint,
    )
    assert main(["test.whoami"]) == 0
    out = capsys.readouterr().out
    assert "thread" in out


def test_cli_status_prints_one_line(ipc_stack, monkeypatch, capsys):
    """``albusctl status`` 给人看的是一行摘要，``--json`` 才是完整快照。"""
    from albuswall.client.cli import main
    from albuswall.client.core import IpcClient

    endpoint: Endpoint = ipc_stack["endpoint"]
    monkeypatch.setattr(IpcClient, "_resolve", lambda self: endpoint)

    assert main(["status"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("ipc listening")
    assert endpoint.address in line
    assert "dispatcher=bound" in line

    assert main(["--json", "status"]) == 0
    payload = capsys.readouterr().out.strip()
    assert '"running":true' in payload


def test_cli_settings_set_requires_assignments(monkeypatch, capsys):
    """没有 KEY=VALUE 时是用法错误，不该跑去连 IPC。"""
    from albuswall.client.cli import EXIT_USAGE, main

    assert main(["settings", "set"]) == EXIT_USAGE
    assert "KEY=VALUE" in capsys.readouterr().err
