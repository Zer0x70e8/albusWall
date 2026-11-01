# -*- coding: utf-8 -*-
"""已有实例提示：尽量非阻塞。"""

from __future__ import annotations

import shutil
import subprocess
import sys

_TITLE = "AlbusWall"
_MESSAGE = "AlbusWall 已在运行中。"


def notify_already_running(
        title: str = _TITLE,
        message: str = _MESSAGE,
) -> None:
    """非阻塞提示已有实例，失败时静默。"""
    try:
        if sys.platform == "win32":
            _notify_win32(title, message)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        else:
            _notify_linux(title, message)
    except Exception:
        pass


def _notify_win32(title: str, message: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    flags = 0x40 | 0x10000 | 0x40000  # INFO | SETFOREGROUND | TOPMOST
    timeout_ms = 3000

    fn = getattr(user32, "MessageBoxTimeoutW", None)
    if fn is None:
        user32.MessageBoxW(None, message, title, flags)
        return

    fn.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.UINT, wintypes.WORD, wintypes.DWORD,
    ]
    fn.restype = ctypes.c_int
    fn(None, message, title, flags, 0, timeout_ms)


def _notify_macos(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}"'
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _notify_linux(title: str, message: str) -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(
            ["notify-send", "-t", "3000", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
