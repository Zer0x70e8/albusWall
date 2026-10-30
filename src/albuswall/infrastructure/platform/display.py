# -*- coding: utf-8 -*-
"""桌面会话探测：尝试连接 X11 / Wayland，而不是只看环境变量。"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def has_desktop() -> bool:
    """当前是否有可用的桌面会话。"""
    if sys.platform == "win32":
        return _win32_has_desktop()
    if sys.platform == "darwin":
        return _darwin_has_desktop()
    return _unix_has_desktop()


def _win32_has_desktop() -> bool:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return bool(user32.GetProcessWindowStation())
    except Exception:
        return False


def _darwin_has_desktop() -> bool:
    # SSH 且无 DISPLAY 时视为无桌面；否则 Aqua 会话存在
    if os.environ.get("SSH_CONNECTION") and not os.environ.get("DISPLAY"):
        return False
    return True


def _unix_has_desktop() -> bool:
    # Wayland：XDG_RUNTIME_DIR 下的 socket
    wayland = os.environ.get("WAYLAND_DISPLAY")
    if wayland:
        if wayland.startswith("/"):
            if _try_connect_unix(wayland):
                return True
        else:
            xdg = os.environ.get("XDG_RUNTIME_DIR")
            if xdg and _try_connect_unix(str(Path(xdg) / wayland)):
                return True
    # X11：/tmp/.X11-unix/X<n>
    display = os.environ.get("DISPLAY")
    if display:
        sock = _x11_socket_path(display)
        if sock and _try_connect_unix(sock):
            return True
    return False


def _x11_socket_path(display: str) -> str | None:
    if display.startswith("/"):
        return display
    if ":" not in display:
        return None
    host, _, rest = display.partition(":")
    if host not in ("", "unix"):
        return None  # 远程 X，不做探测
    num = rest.split(".", 1)[0]
    if not num.isdigit():
        return None
    return f"/tmp/.X11-unix/X{num}"


def _try_connect_unix(path: str, timeout: float = 0.5) -> bool:
    if not Path(path).exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False
