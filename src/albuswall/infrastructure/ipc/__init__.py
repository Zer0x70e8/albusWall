#
"""AlbusWall IPC 基础设施。

提供：
- 跨平台端点：Unix Domain Socket / Windows 命名管道
- authkey 认证
- 单实例锁：bind 端点成功即锁；失败时区分真实例与僵尸 socket
- JSON 帧协议：send_bytes/recv_bytes，不用 pickle
- 内置 ping：在连接线程直接回，不依赖任何业务层
- 客户端：连接、带超时的请求/响应（call / call_raw）
- probe_instance：探活，返回元信息或 None

不提供：命令注册、命令路由、主线程调度、CLI。
业务侧若需要处理请求，调用 service.set_handler(fn)；
fn(req: dict) -> Any，req 已过版本校验、未做业务语义解析，
返回值作为 result。未设置 handler 时，非 ping 请求返回 no_handler。
"""
from __future__ import annotations

from .__about__ import __version__
from ._client import IpcClient
from ._service import EndpointService
from ._probe import probe_instance, InstanceKind, classify_instance
from ._errors import (
    IpcError, IpcProtocolError, IpcUnavailable, IpcTimeout,
    IpcRemoteError, InstanceAlreadyRunning,
)
from ._endpoints import get_endpoint, get_authkey
from ._constants import APP_NAME, PROTOCOL_VERSION, MAX_FRAME

# 容器接入层
from .manager import IpcManager, register_ipc

__all__ = [
    "__version__",
    "APP_NAME", "PROTOCOL_VERSION", "MAX_FRAME",
    "IpcError", "IpcProtocolError", "IpcUnavailable", "IpcTimeout",
    "IpcRemoteError", "InstanceAlreadyRunning",
    "get_endpoint", "get_authkey",
    "EndpointService", "IpcClient",
    "probe_instance", "InstanceKind", "classify_instance",
    "IpcManager", "register_ipc",
]
