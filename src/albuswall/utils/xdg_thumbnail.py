#
"""XDG 缩略图规范工具。

实现 freedesktop.org Thumbnail Managing Standard：
https://specifications.freedesktop.org/thumbnail-spec/thumbnail-spec-latest.html

设计契约
--------
* **纯计算 + PNG 元数据读取**。本模块不写任何文件。
* 所有 IO / 解析失败都返回 ``None`` / ``False``，不抛异常。
  XDG 缓存是跨应用共享目录，第三方应用写的 PNG 不保证符合规范。
* 平台判断集中在 :func:`is_xdg_thumbnail_supported`。是否启用由
  bootstrap 决定；本模块只提供"能不能用"的事实。

路径约定
--------
::

    $XDG_CACHE_HOME/thumbnails/
    ├── normal/      128 px
    ├── large/       256 px
    ├── x-large/     512 px
    ├── xx-large/    1024 px
    └── fail/        生成失败的标记文件

文件名 = ``md5(file_uri(abs_path))`` + ``.png``，小写十六进制。
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Optional

# ---------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------

XdgSpec = Literal["normal", "large", "x-large", "xx-large"]

XDG_SPECS: Final[tuple[XdgSpec, ...]] = (
    "normal", "large", "x-large", "xx-large",
)

# 协议规定的最大边长（像素）。仅供调用方参考；本模块不生成缩略图。
XDG_SPEC_PIXELS: Final[dict[XdgSpec, int]] = {
    "normal": 128,
    "large": 256,
    "x-large": 512,
    "xx-large": 1024,
}

# 协议强制权限
XDG_DIR_MODE: Final[int] = 0o700
XDG_FILE_MODE: Final[int] = 0o600

__all__ = [
    "XdgSpec",
    "XDG_SPECS",
    "XDG_SPEC_PIXELS",
    "XDG_DIR_MODE",
    "XDG_FILE_MODE",
    "is_xdg_thumbnail_supported",
    "xdg_cache_home",
    "xdg_thumbnails_root",
    "file_uri",
    "uri_to_path",
    "thumbnail_filename",
    "xdg_thumbnail_path",
    "xdg_fail_path",
    "XdgThumbnailInfo",
    "read_thumbnail_info",
    "is_thumbnail_valid_for",
    "is_inside_xdg_cache",
]


# ---------------------------------------------------------------------
# 平台判断
# ---------------------------------------------------------------------

def is_xdg_thumbnail_supported() -> bool:
    """XDG 缩略图协议是否在本平台上有实际意义。

    * Linux / BSD  → True （协议原生适用）
    * macOS        → False（QuickLook 使用私有格式的 index.db）
    * Windows      → False（thumbcache_*.db 为 ESE 数据库）

    其它平台（如 WASI）也返回 False。
    """
    return sys.platform.startswith(
        ("linux", "freebsd", "openbsd", "netbsd", "dragonfly")
    )


# ---------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------

def xdg_cache_home() -> Path:
    """``$XDG_CACHE_HOME``，未设置时回退到 ``~/.cache``。

    注意：这只返回"XDG 规范定义的位置"，不代表本平台支持 XDG。
    想要应用私有缓存目录请用 ``utils.platform_paths.platform_cache_root``。
    """
    raw = os.environ.get("XDG_CACHE_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".cache"


def xdg_thumbnails_root() -> Path:
    """XDG 缩略图缓存根目录（不创建）。"""
    return xdg_cache_home() / "thumbnails"


# ---------------------------------------------------------------------
# URI ↔ 路径
# ---------------------------------------------------------------------

def file_uri(abs_path: str | Path) -> str:
    """绝对路径 → XDG 规范要求的 ``file://`` URI。

    * 非 ASCII 与保留字符走百分号编码（保留 ``/`` 和 ``:``）
    * Windows 盘符按 ``file:///C:/Users/...``（前导斜杠由协议规定）
    * 反斜杠统一替换为正斜杠
    """
    p = Path(abs_path).resolve()
    posix = p.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    encoded = urllib.parse.quote(posix, safe="/:")
    return "file://" + encoded


def uri_to_path(uri: str) -> Optional[str]:
    """``file://`` URI 反解为本地路径；非 file 协议返回 ``None``。"""
    try:
        parsed = urllib.parse.urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    # Windows：去掉盘符前多余的 "/"
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


# ---------------------------------------------------------------------
# 缩略图路径计算
# ---------------------------------------------------------------------

def thumbnail_filename(abs_path: str | Path) -> str:
    """源文件绝对路径 → XDG 缩略图文件名（含 ``.png``）。

    = ``md5(file_uri(abs_path)) + ".png"``，小写十六进制。
    """
    uri = file_uri(abs_path)
    digest = hashlib.md5(uri.encode("utf-8")).hexdigest()
    return f"{digest}.png"


def xdg_thumbnail_path(
        abs_path: str | Path,
        spec: XdgSpec = "large",
        *,
        root: Optional[Path] = None,
) -> Path:
    """该源文件在 XDG 缓存中的缩略图路径（不检查存在性）。"""
    base = root if root is not None else xdg_thumbnails_root()
    return base / spec / thumbnail_filename(abs_path)


def xdg_fail_path(
        abs_path: str | Path,
        *,
        root: Optional[Path] = None,
) -> Path:
    """该源文件在 XDG ``fail/`` 目录下的失败标记路径。"""
    base = root if root is not None else xdg_thumbnails_root()
    return base / "fail" / thumbnail_filename(abs_path)


# ---------------------------------------------------------------------
# PNG 元数据
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class XdgThumbnailInfo:
    """从 PNG 内嵌元数据读出的 XDG 信息。

    协议要求 ``Thumb::URI`` 与 ``Thumb::MTime`` 必须存在；
    缺失时对应字段为 ``None``，调用方自行判断是否可用。
    """
    path: Path
    uri: Optional[str]
    mtime: Optional[int]
    size: Optional[int]


def read_thumbnail_info(png_path: Path) -> Optional[XdgThumbnailInfo]:
    """读取 PNG 内嵌的 XDG 元数据。

    任何失败（文件不存在 / 非 PNG / Pillow 未安装 / 元数据损坏）
    都返回 ``None``——调用方按"无 XDG 兼容"处理。
    """
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
    except Exception:  # noqa: BLE001 —— 见模块 docstring
        return None


def is_thumbnail_valid_for(png_path: Path, source_abs: str | Path) -> bool:
    """判断 XDG 缩略图是否仍对某源文件有效。

    有效判据（协议规定）：
      * ``Thumb::URI`` 存在
      * ``Thumb::MTime`` 存在且等于源文件当前 mtime（整数秒）

    mtime 不一致说明源文件已被修改，缩略图过期必须重建。
    """
    info = read_thumbnail_info(png_path)
    if info is None or info.uri is None or info.mtime is None:
        return False
    try:
        st = os.stat(source_abs)
    except OSError:
        return False
    return int(st.st_mtime) == info.mtime


# ---------------------------------------------------------------------
# 所有权校验
# ---------------------------------------------------------------------

def is_inside_xdg_cache(
        path: str | Path,
        *,
        root: Optional[Path] = None,
) -> bool:
    """判断路径是否落在 XDG 缩略图缓存内部。

    用途：防止私有 ``thumb_path`` 误配到 XDG 根时被当作私有数据删除。
    XDG 缓存是跨应用共享的，删除会影响其它应用。
    """
    base = (root if root is not None else xdg_thumbnails_root()).resolve()
    try:
        Path(path).resolve().relative_to(base)
        return True
    except (ValueError, OSError):
        return False
