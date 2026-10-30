#
"""端点定位与 authkey 管理。

端点位置（按系统用户隔离）：
- Unix:    $XDG_RUNTIME_DIR/albuswall/ipc.sock
           （回退 ~/.cache/albuswall/run-<uid>/ipc.sock）
- Windows: \\\\.\\pipe\\albuswall-<user>
authkey:   同目录 authkey 文件，0600。
"""
from __future__ import annotations

import os
import sys
import getpass
import secrets
import tempfile
from pathlib import Path

from ._constants import APP_NAME


def _runtime_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP_NAME
    uid = os.getuid() if hasattr(os, "getuid") else "u"
    return Path.home() / ".cache" / APP_NAME / f"run-{uid}"


def get_endpoint() -> str:
    if sys.platform == "win32":
        user = "".join(c for c in getpass.getuser() if c.isalnum() or c in "-_")
        return rf"\\.\pipe\{APP_NAME}-{user}"
    return str(_runtime_dir() / "ipc.sock")


def _authkey_path() -> Path:
    return _runtime_dir() / "authkey"


def get_authkey(create: bool = False) -> bytes | None:
    """create=False 时缺失返回 None（probe 用）；create=True 时确保存在。"""
    path = _authkey_path()
    if path.exists():
        try:
            data = path.read_bytes().strip()
        except OSError:
            data = b""
        if data:
            return data
        if not create:
            return None
    if not create:
        return None

    _runtime_dir().mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return get_authkey(create=False)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return key
