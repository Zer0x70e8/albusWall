#
"""XDG 缩略图规范工具。

实现 freedesktop.org Thumbnail Managing Standard：
https://specifications.freedesktop.org/thumbnail-spec/thumbnail-spec-latest.html

跨平台约定
----------
**协议语义全平台一致**（目录布局、md5 文件名、PNG 内嵌元数据、fail
标记、0700/0600 权限）。唯一随平台变化的是"缓存根目录"：

    Linux/BSD → $XDG_CACHE_HOME 或 ~/.cache
    macOS     → ~/Library/Caches
    Windows   → %LOCALAPPDATA% 或 ~/AppData/Local
    其它      → ~/.cache

因此 ``$CACHE/thumbnails/`` 在三个平台上的**内容布局完全相同**。
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Optional

XdgSpec = Literal["normal", "large", "x-large", "xx-large"]

XDG_SPECS: Final[tuple[XdgSpec, ...]] = (
    "normal", "large", "x-large", "xx-large",
)

XDG_SPEC_PIXELS: Final[dict[XdgSpec, int]] = {
    "normal": 128,
    "large": 256,
    "x-large": 512,
    "xx-large": 1024,
}

XDG_DIR_MODE: Final[int] = 0o700
XDG_FILE_MODE: Final[int] = 0o600

__all__ = [
    "XdgSpec", "XDG_SPECS", "XDG_SPEC_PIXELS",
    "XDG_DIR_MODE", "XDG_FILE_MODE",
    "is_xdg_thumbnail_supported",
    "xdg_cache_home", "xdg_thumbnails_root",
    "file_uri", "uri_to_path",
    "thumbnail_filename", "xdg_thumbnail_path", "xdg_fail_path",
    "XdgThumbnailInfo", "read_thumbnail_info", "is_thumbnail_valid_for",
    "is_inside_xdg_cache",
]


# ---------------------------------------------------------------------
# 平台判断
# ---------------------------------------------------------------------

def is_xdg_thumbnail_supported() -> bool:
    """是否启用 XDG 缩略图协议。

    协议语义（布局/文件名/元数据/权限）全平台适用，因此恒 True。
    根目录按平台惯例选择，见 :func:`xdg_cache_home`。
    """
    return True


# ---------------------------------------------------------------------
# 平台缓存根目录
# ---------------------------------------------------------------------

def _linux_cache_home() -> Path:
    """XDG Base Directory：$XDG_CACHE_HOME，未设置/为空/相对路径时
    按规范回退到 ~/.cache。"""
    raw = os.environ.get("XDG_CACHE_HOME")
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():  # XDG 规范：相对路径无效
            return p
    return Path.home() / ".cache"


def _macos_cache_home() -> Path:
    return Path.home() / "Library" / "Caches"


def _windows_cache_home() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def xdg_cache_home() -> Path:
    """平台缓存根目录（不含 ``thumbnails`` 子目录）。

    注意：这是"XDG 协议在这里落地"的根，不代表 XDG Base Directory 规范。
    """
    if os.name == "nt":
        return _windows_cache_home()
    if sys.platform == "darwin":
        return _macos_cache_home()
    return _linux_cache_home()


def xdg_thumbnails_root() -> Path:
    """XDG 缩略图缓存根目录（不创建）。"""
    return xdg_cache_home() / "thumbnails"


# ---------------------------------------------------------------------
# URI ↔ 路径（保持不变）
# ---------------------------------------------------------------------

def file_uri(abs_path: str | Path) -> str:
    p = Path(abs_path).resolve()
    posix = p.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    encoded = urllib.parse.quote(posix, safe="/:")
    return "file://" + encoded


def uri_to_path(uri: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


# ---------------------------------------------------------------------
# 缩略图路径计算（保持不变）
# ---------------------------------------------------------------------

def thumbnail_filename(abs_path: str | Path) -> str:
    uri = file_uri(abs_path)
    digest = hashlib.md5(uri.encode("utf-8")).hexdigest()
    return f"{digest}.png"


def xdg_thumbnail_path(
        abs_path: str | Path,
        spec: XdgSpec = "large",
        *,
        root: Optional[Path] = None,
) -> Path:
    base = root if root is not None else xdg_thumbnails_root()
    return base / spec / thumbnail_filename(abs_path)


def xdg_fail_path(
        abs_path: str | Path,
        root: Optional[Path] = None,
) -> Path:
    """永久失败标记路径：``<root>/fail/<md5(file_uri)>.png``。

    ``root`` 参数化是本次重构的关键：私有模式与 XDG 模式共用同一套
    布局，唯一差异就是根目录常量 —— 由调用方（ThumbnailStorage）注入
    自己持有的根，而不是在这里猜。

    ``root`` 缺省回退到 :func:`xdg_thumbnails_root()`，保持既有调用点
    （不传 root 的）行为不变。位置参数与关键字参数都接受。
    """
    base = Path(root) if root is not None else xdg_thumbnails_root()
    return base / "fail" / thumbnail_filename(abs_path)


# ---------------------------------------------------------------------
# PNG 元数据（保持不变）
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class XdgThumbnailInfo:
    path: Path
    uri: Optional[str]
    mtime: Optional[int]
    size: Optional[int]


def read_thumbnail_info(png_path: Path) -> Optional[XdgThumbnailInfo]:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(png_path) as im:
            info = im.info or {}
            mtime_raw = info.get("Thumb::MTime")
            size_raw = info.get("Thumb::Size")
            return XdgThumbnailInfo(
                path=png_path,
                uri=info.get("Thumb::URI"),
                mtime=int(mtime_raw) if mtime_raw is not None else None,
                size=int(size_raw) if size_raw is not None else None,
            )
    except Exception:  # noqa: BLE001
        return None


def is_thumbnail_valid_for(png_path: Path, source_abs: str | Path) -> bool:
    info = read_thumbnail_info(png_path)
    if info is None or info.uri is None or info.mtime is None:
        return False
    try:
        st = os.stat(source_abs)
    except OSError:
        return False
    return int(st.st_mtime) == info.mtime


# ---------------------------------------------------------------------
# 所有权校验（保持不变）
# ---------------------------------------------------------------------

def is_inside_xdg_cache(
        path: str | Path,
        *,
        root: Optional[Path] = None,
) -> bool:
    base = (root if root is not None else xdg_thumbnails_root()).resolve()
    try:
        Path(path).resolve().relative_to(base)
        return True
    except (ValueError, OSError):
        return False
