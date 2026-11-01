#
"""〘前置能力〙退出码透传：``DispatchError.exit_code`` → IPC 响应 → 客户端异常。

覆盖四条：
  1. 业务异常 → 响应携带 ``exit_code`` → 客户端 ``IpcRemoteError.exit_code`` 一致
  2. 纯 IPC 层错误（无建议退出码）→ 响应不带该字段 → 客户端属性为 ``None``
  3. 业务层自己没传 ``exit_code`` 时，IPC 层**不再**从 ``__cause__`` 回捞
     （钉住 ``_service.py`` 里旧 ``_exit_code_of`` 异常链技巧已被移除）
  4. ``IpcRemoteError`` 构造签名向后兼容（message / code 位置与关键字不变）

隔离约束：每个用例在临时目录里 bind 一条 Unix socket（win32 用命名管道），
authkey 显式传入 —— 不碰 ``~/.local/share``，也不依赖主实例是否在跑。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from albuswall.command import Command, CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import ExitCode
from albuswall.infrastructure.ipc import (
    EndpointService,
    IpcClient,
    IpcError,
    IpcRemoteError,
)
from albuswall.infrastructure.ipc._protocol import recv, send
from albuswall.service.command.ipc_request_service import IpcRequestService
from albuswall.service.command.service import CommandService

_AUTHKEY = b"albuswall-test-authkey"

_CALL_TIMEOUT = 5.0


# ==================== 端点 / 客户端辅助 ====================

def _endpoint_path(tmp_dir: Path) -> str:
    if sys.platform == "win32":
        return rf"\\.\pipe\albuswall-test-{uuid4().hex}"
    return str(tmp_dir / "ep.sock")


@pytest.fixture
def ipc_endpoint():
    """已 bind + start 的端点、端点服务本身，以及「挂上命令服务」的装配函数。

    yield ``(endpoint, service, serve)``：``serve(command_service)`` 装好
    ``IpcRequestService``（正是生产链路的一层）；不调用 ``serve`` 就保持
    「无 handler」，用于覆盖 ``no_handler`` 分支。需要绕过 Dispatcher 直接
    给 ``_service.py`` 喂异常时用 ``service.set_handler(...)``。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="aw-ipc-"))
    endpoint = _endpoint_path(tmp_dir)
    service = EndpointService(endpoint=endpoint, authkey=_AUTHKEY)
    service.bind()
    service.start()
    adapters: list[IpcRequestService] = []

    def serve(command_service: CommandService) -> IpcRequestService:
        adapter = IpcRequestService(service, command_service)
        adapter.start()
        adapters.append(adapter)
        return adapter

    try:
        yield endpoint, service, serve
    finally:
        for adapter in adapters:
            adapter.shutdown()
        service.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _command_service(func: Callable[[], Any], *, path: str = "boom") -> CommandService:
    registry = CommandRegistry()
    registry.register(Command(path=path, func=func, help="测试用命令"))
    return CommandService(registry)


def _wire_call(endpoint: str, req: dict) -> dict:
    """绕过 ``IpcClient``，直接收发一帧 —— 用例要断言**线上**字段。"""
    from multiprocessing.connection import Client

    with Client(endpoint, authkey=_AUTHKEY) as conn:
        send(conn, dict(req))
        assert conn.poll(_CALL_TIMEOUT), "no response frame"
        return recv(conn)


def _client_error(endpoint: str, method: str) -> IpcRemoteError:
    with IpcClient(
        endpoint=endpoint, authkey=_AUTHKEY, timeout=_CALL_TIMEOUT,
    ) as c, pytest.raises(IpcRemoteError) as excinfo:
        c.call(method)
    return excinfo.value


# ==================== 1. exit_code 穿过 IPC 边界 ====================

def test_dispatch_error_exit_code_crosses_wire_and_client(ipc_endpoint):
    endpoint, _service, serve = ipc_endpoint

    def boom() -> dict:
        raise BadRequest("bad params for test")

    serve(_command_service(boom))

    resp = _wire_call(endpoint, {"method": "boom", "params": {}})
    assert resp["ok"] is False
    assert resp["code"] == "bad_request"
    # 服务端把「异常类自己声明的 exit_code」原样带出来
    assert resp["exit_code"] == int(ExitCode.USAGE_ERROR)

    err = _client_error(endpoint, "boom")
    assert err.code == "bad_request"
    assert err.exit_code == int(ExitCode.USAGE_ERROR)
    # 线上是 JSON 数字（int），客户端拿到的也必须是 int 而不是 ExitCode 实例
    assert type(err.exit_code) is int


# ==================== 2. 纯 IPC 层错误没有退出码 ====================

def test_pure_ipc_error_has_no_exit_code(ipc_endpoint):
    endpoint, _service, _serve = ipc_endpoint          # 故意不装 handler

    resp = _wire_call(endpoint, {"method": "boom", "params": {}})
    assert resp["ok"] is False
    assert resp["code"] == "no_handler"
    assert "exit_code" not in resp

    err = _client_error(endpoint, "boom")
    assert err.code == "no_handler"
    assert err.exit_code is None


# ==================== 3. 不再沿异常链回捞 ====================

def test_endpoint_service_does_not_borrow_exit_code_from_cause(ipc_endpoint):
    """``exit_code`` 只认异常自身属性，不从 ``__cause__`` / ``__context__`` 捞。

    回归保护：``_service.py`` 曾用 ``_exit_code_of`` 沿因果链找最近一个带
    ``exit_code`` 的异常。现在值由抛出方在构造 ``IpcRemoteError`` 时显式传入，
    因此「忘了传」必须表现为**没有退出码**，而不是悄悄在链上捡一个。

    这里直接给端点服务喂异常（绕过 ``Dispatcher``）：Dispatcher 会把非
    ``DispatchError`` 包装成 ``CommandFailed``，那样测不到这条分支。
    """
    endpoint, service, _serve = ipc_endpoint

    class _CarrierError(Exception):
        exit_code = int(ExitCode.CONFIG_ERROR)

    def handler(req: dict) -> dict:
        try:
            raise _CarrierError("inner")
        except _CarrierError as exc:
            raise IpcRemoteError("outer failure") from exc

    service.set_handler(handler)

    resp = _wire_call(endpoint, {"method": "boom", "params": {}})
    assert resp["ok"] is False
    assert resp["code"] == "remote_error"
    assert "exit_code" not in resp


def test_endpoint_service_forwards_explicit_exit_code(ipc_endpoint):
    """反面：显式传了就原样透出（同一条 ``getattr`` 通路）。"""
    endpoint, service, _serve = ipc_endpoint

    def handler(req: dict) -> dict:
        raise IpcRemoteError(
            "explicit", code="bad_request", exit_code=int(ExitCode.CONFIG_ERROR),
        )

    service.set_handler(handler)

    resp = _wire_call(endpoint, {"method": "boom", "params": {}})
    assert resp["code"] == "bad_request"
    assert resp["exit_code"] == int(ExitCode.CONFIG_ERROR)


# ==================== 4. 构造签名向后兼容 ====================

def test_ipc_remote_error_signature_is_backwards_compatible():
    bare = IpcRemoteError("boom")
    assert isinstance(bare, IpcError)
    assert bare.args == ("boom",)
    assert bare.code == "remote_error"
    assert bare.exit_code is None

    # 旧调用形态：第二个参数仍可作关键字传入
    coded = IpcRemoteError("boom", code="bad_request")
    assert coded.code == "bad_request"
    assert coded.exit_code is None

    # 新形态：第三个关键字参数
    with_exit = IpcRemoteError("boom", code="bad_request", exit_code=64)
    assert with_exit.exit_code == 64
    assert str(with_exit) == "boom"
