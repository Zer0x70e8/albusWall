#
"""JSON 帧编解码（不使用 pickle）。"""
from __future__ import annotations

import json

from ._constants import MAX_FRAME, PROTOCOL_VERSION
from ._errors import IpcProtocolError, IpcUnavailable

__all__ = ["send", "recv", "safe_send"]


def send(conn, obj: dict) -> None:
    obj.setdefault("v", PROTOCOL_VERSION)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise IpcProtocolError(f"frame too large: {len(data)}")
    conn.send_bytes(data)


def recv(conn) -> dict:
    try:
        data = conn.recv_bytes(MAX_FRAME)
    except EOFError as e:
        raise IpcUnavailable("connection closed by peer") from e
    except OSError as e:
        raise IpcUnavailable(f"recv failed: {e}") from e

    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IpcProtocolError(f"invalid frame: {e}") from e
    if not isinstance(obj, dict):
        raise IpcProtocolError("frame must be a JSON object")
    return obj


# noinspection broad-exception
def safe_send(conn, obj) -> None:
    try:
        send(conn, obj)
    except Exception:
        pass
