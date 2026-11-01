#
"""探活。"""

from __future__ import annotations

from enum import Enum

from ._endpoints import get_endpoint, get_authkey
from ._errors import (
    IpcTimeout,
    IpcUnavailable,
    IpcRemoteError,
    IpcProtocolError,
    IpcError,
)
from ._client import IpcClient


def probe_instance(endpoint: str | None = None,
                   authkey: bytes | None = None,
                   timeout: float = 1.5) -> dict | None:
    """探活。

    返回 dict 表示存活（可能是 stalled=True 的保守判定）；
    返回 None 表示无实例。

    IpcTimeout → 视为存活：端点在手但业务卡住，仍不应允许开第二个实例。
    IpcUnavailable / IpcRemoteError → 视为无实例。
    """
    endpoint = endpoint or get_endpoint()
    authkey = authkey if authkey is not None else get_authkey(create=False)
    if authkey is None:
        return None
    try:
        with IpcClient(endpoint, authkey, timeout=timeout) as c:
            return c.ping(timeout=timeout)
    except IpcTimeout:
        return {"pid": None, "stalled": True, "endpoint": endpoint}
    except (IpcUnavailable, IpcRemoteError, IpcProtocolError):
        return None
    except IpcError:
        return None


class InstanceKind(Enum):
    NONE = "none"
    ALIVE = "alive"
    STALLED = "stalled"


def classify_instance(timeout: float = 1.5) -> InstanceKind:
    """把 probe_instance 的返回值映射为状态枚举。

    - NONE    : 无实例，可以启动 main
    - ALIVE   : 有实例且响应正常
    - STALLED : 端点在手但业务卡住（保守判为存活，不允许再开第二个）
    """
    info = probe_instance(timeout=timeout)
    if info is None:
        return InstanceKind.NONE
    if info.get("stalled"):
        return InstanceKind.STALLED
    return InstanceKind.ALIVE
