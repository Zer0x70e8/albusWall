#
"""进程间调用协议：帧编解码 + 错误对象 + 两端共用的异常。

本模块**只有数据与字符串**：不做 IO、不 import core / services /
infrastructure，因此 CLI 进程可以只导入 ``common.ipc`` 与
``utils.ipc_endpoint`` 就完成一次调用，不会把整个应用拖起来。

帧格式
------
一行一个 UTF-8 JSON 对象（JSON-RPC 2.0 风格），语义上与传输层无关：

请求::

    {"jsonrpc": "2.0", "id": 7, "method": "source.list", "params": {}}

成功响应::

    {"jsonrpc": "2.0", "id": 7, "result": {...}}

失败响应::

    {"jsonrpc": "2.0", "id": 7,
     "error": {"code": -32601, "message": "method not found: 'x'"}}

服务端主动推送（无 ``id``）::

    {"jsonrpc": "2.0", "method": "event",
     "params": {"topic": "import.finished", "data": {...}}}

为什么不用 pickle
-----------------
``multiprocessing.connection`` 的 ``send`` / ``recv`` 默认走 pickle，
反序列化即执行任意代码。本协议一律走 ``send_bytes`` / ``recv_bytes``
搬运 JSON：跨进程边界上只看数据，不看对象。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from albuswall.common.enums import IpcErrorCode

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "EVENT_METHOD",
    "JSONRPC",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "CallTimeout",
    "InvalidParams",
    "IpcProtocolError",
    "MethodNotFound",
    "RemoteError",
    "Unavailable",
    "decode_frame",
    "encode_frame",
    "error_object",
    "make_error",
    "make_event",
    "make_request",
    "make_result",
]

#: 握手时两端交换的协议版本；不匹配时服务端拒绝连接。
PROTOCOL_VERSION = 1

JSONRPC = "2.0"

#: 单帧上限。本地 socket 也要设上限，否则对端可以让服务端吃满内存。
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: 默认单次调用超时（秒）。包含「等待主循环排到这个回调」+「执行」两段。
DEFAULT_CALL_TIMEOUT = 30.0

#: 服务端推送事件的固定 method 名。
EVENT_METHOD = "event"


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class IpcProtocolError(Exception):
    """服务端侧错误的基类：``code`` 决定响应里的 JSON-RPC error.code。"""

    code: IpcErrorCode = IpcErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


class MethodNotFound(IpcProtocolError):
    code = IpcErrorCode.METHOD_NOT_FOUND


class InvalidParams(IpcProtocolError):
    code = IpcErrorCode.INVALID_PARAMS


class CallTimeout(IpcProtocolError):
    code = IpcErrorCode.TIMEOUT


class Unavailable(IpcProtocolError):
    code = IpcErrorCode.UNAVAILABLE


class RemoteError(Exception):
    """客户端侧：把远端响应里的 ``error`` 对象还原成本地异常。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    @classmethod
    def from_object(cls, error: Mapping[str, Any]) -> RemoteError:
        raw_code = error.get("code", IpcErrorCode.INTERNAL_ERROR)
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = int(IpcErrorCode.INTERNAL_ERROR)
        return cls(code, str(error.get("message", "unknown error")), error.get("data"))


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #
def _fallback(value: Any) -> Any:
    """``json.dumps(default=...)`` 的兜底：让 DTO / Path / Enum 也能出去。

    宁可丢精度也不要因为一个 ``datetime`` 就让整次调用失败：
    这些值只用于展示，调用方不该依赖它们的类型。
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)  # type: ignore[arg-type]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def encode_frame(message: Mapping[str, Any]) -> bytes:
    """把消息编码为一帧字节。"""
    return json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_fallback,
    ).encode("utf-8")


def decode_frame(raw: bytes) -> dict[str, Any]:
    """把一帧字节解码为消息 dict。

    :raises ValueError: 不是合法 JSON，或顶层不是对象
    """
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"frame must be a JSON object, got {type(obj).__name__}")
    return obj


# --------------------------------------------------------------------------- #
# 消息构造
# --------------------------------------------------------------------------- #
def make_request(mid: Any, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC,
        "id": mid,
        "method": method,
        "params": dict(params or {}),
    }


def make_result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": mid, "result": result}


def error_object(
        code: int | IpcErrorCode,
        message: str,
        data: Any = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        obj["data"] = data
    return obj


def make_error(
        mid: Any,
        code: int | IpcErrorCode,
        message: str,
        data: Any = None,
) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": mid, "error": error_object(code, message, data)}


def make_event(topic: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"topic": topic}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": JSONRPC, "method": EVENT_METHOD, "params": payload}
