#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
deinfo - 桌面环境信息探测工具 (Desktop Environment Info)

跨平台探测当前桌面环境 / 窗口管理器，支持 Linux (X11 / Wayland)，
对 Hyprland 等从 .bash_profile 裸启动的场景做了专门兼容。

公共 API:
    get_desktop_info()             -> DesktopInfo  (完整信息，带缓存)
    is_tiling()                    -> bool
    is_hyprland()                  -> bool
    is_wayland()                   -> bool
    is_window_buttons_left()       -> Optional[bool]
    should_hide_window_controls()  -> bool

命令行:
    python -m deinfo
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

__all__ = [
    "DesktopInfo",
    "get_desktop_info",
    "is_tiling",
    "is_hyprland",
    "is_wayland",
    "is_window_buttons_left",
    "should_hide_window_controls",
]

__version__ = "0.2.0"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 已知的平铺式窗口管理器 / 合成器（小写）
TILING_WMS: frozenset[str] = frozenset({
    "hyprland", "sway", "i3", "bspwm", "dwm", "dwl",
    "awesome", "qtile", "xmonad", "herbstluftwm",
    "ratpoison", "stumpwm", "spectacle", "wayfire",
    "monsterwm", "wmfs", "wmii", "yabai", "amethyst",
})

# GNOME 系 button-layout 中认定为"按钮"的标识
_BUTTON_NAMES: frozenset[str] = frozenset({
    "close", "minimize", "maximize", "appmenu",
})


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DesktopInfo:
    """桌面环境 / 窗口管理器探测结果（不可变）。"""
    name: str  # 归一化后的名称（小写），未知为 "unknown"
    raw_desktop: str  # XDG_CURRENT_DESKTOP 原始值
    session_type: str  # "wayland" | "x11" | "unknown"
    is_hyprland: bool  # 是否明确运行在 Hyprland 下
    is_tiling: bool  # 是否为平铺式窗口管理器
    buttons_left: Optional[bool]  # True=左, False=右, None=无法判断
    should_hide_window_controls: bool  # 是否倾向于隐藏窗口控制按钮（CSD）


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _normalize_name(raw: str) -> str:
    """把 XDG 变量里可能带冒号、发行版后缀的名字归一化。"""
    if not raw:
        return ""
    first = raw.split(":")[0].strip()  # "ubuntu:GNOME" -> "GNOME"
    name = first.lower()
    for suffix in ("-classic", "-flashback"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _detect_wm_from_wmctrl() -> Optional[str]:
    """如果装了 wmctrl，用它获取准确的 WM 名称。"""
    if shutil.which("wmctrl") is None:
        return None
    try:
        out = subprocess.check_output(
            ["wmctrl", "-m"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        if line.startswith("Name:"):
            return line.split(":", 1)[1].strip()
    return None


def _detect_buttons_left() -> Optional[bool]:
    """
    仅对使用 GNOME 系 button-layout 的环境有效。
    gsettings button-layout 格式: "左侧按钮:右侧按钮"
    例如 "'appmenu:minimize,maximize,close'" -> 按钮在右
         "'close,minimize,maximize:'"       -> 按钮在左
    """
    if shutil.which("gsettings") is None:
        return None
    try:
        out = subprocess.check_output(
            ["gsettings", "get",
             "org.gnome.desktop.wm.preferences", "button-layout"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None

    layout = out.strip("'\"")
    if ":" not in layout:
        return None

    left_part, _ = layout.split(":", 1)
    left_buttons = [b.strip() for b in left_part.split(",") if b.strip()]
    return any(b in _BUTTON_NAMES for b in left_buttons)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_desktop_info() -> DesktopInfo:
    """探测并缓存当前桌面环境信息。"""
    raw_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    name = _normalize_name(raw_desktop)

    if not name:
        for key in ("XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            name = _normalize_name(os.environ.get(key, ""))
            if name:
                break

    if not name:
        wmctrl_name = _detect_wm_from_wmctrl()
        if wmctrl_name:
            name = wmctrl_name.lower()

    # Hyprland 最确凿证据：实例签名环境变量
    is_hyprland_ = "HYPRLAND_INSTANCE_SIGNATURE" in os.environ
    if is_hyprland:
        name = "hyprland"

    # 平铺式判断
    is_tiling_ = is_hyprland_ or name in TILING_WMS

    # 会话类型
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type not in ("wayland", "x11"):
        session_type = "unknown"

    # 窗口按钮探测：仅对非平铺环境有意义
    if is_tiling_:
        # 平铺式 WM 剥离客户端装饰，无固定按钮位置可言
        buttons_left: Optional[bool] = None
        should_hide = True
    else:
        buttons_left = _detect_buttons_left()
        # 堆叠式桌面默认保留系统标题栏，不主动隐藏
        should_hide = False

    return DesktopInfo(
        name=name or "unknown",
        raw_desktop=raw_desktop,
        session_type=session_type,
        is_hyprland=is_hyprland_,
        is_tiling=is_tiling_,
        buttons_left=buttons_left,
        should_hide_window_controls=should_hide,
    )


def is_tiling() -> bool:
    """当前是否为平铺式窗口管理器。"""
    return get_desktop_info().is_tiling


def is_hyprland() -> bool:
    """当前是否运行在 Hyprland 下（依赖实例签名变量）。"""
    return get_desktop_info().is_hyprland


def is_wayland() -> bool:
    """当前会话是否为 Wayland。"""
    return get_desktop_info().session_type == "wayland"


def is_window_buttons_left() -> Optional[bool]:
    """
    窗口按钮是否左对齐。
    - True/False: 已知结果（GNOME 系）
    - None: 无法判断（非 GNOME 系，或是平铺式 WM）
    """
    return get_desktop_info().buttons_left


def should_hide_window_controls() -> bool:
    """
    是否应当隐藏窗口控制按钮（客户端装饰 CSD）。

    平铺式窗口管理器（Hyprland / Sway / i3 等）通常剥离 CSD，
    此时返回 True；堆叠式桌面返回 False。
    """
    return get_desktop_info().should_hide_window_controls


# ---------------------------------------------------------------------------
# 命令行入口: python -m deinfo
# ---------------------------------------------------------------------------

def _main() -> None:
    info = get_desktop_info()
    btn_map = {True: "左对齐", False: "右对齐", None: "无法判断"}

    print(f"桌面环境 / WM : {info.name}")
    print(f"原始 XDG 值   : {info.raw_desktop!r}")
    print(f"会话类型      : {info.session_type}")
    print(f"Hyprland      : {info.is_hyprland}")
    print(f"平铺式        : {info.is_tiling}")
    print(f"窗口按钮      : {btn_map[info.buttons_left]}")
    print(f"应隐藏控制按钮: {info.should_hide_window_controls}")


if __name__ == "__main__":
    _main()
