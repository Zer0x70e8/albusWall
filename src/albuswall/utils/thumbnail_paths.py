#
"""跨平台缩略图根目录解析。

平台规则：
- 非 Darwin 的 Unix（Linux / *BSD / Solaris 等 posix）：走 XDG 缩略图规范。
- Darwin / NT：放软件自己的缓存目录。

本模块只做平台判断与路径计算，不做 IO、不读配置系统。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from albuswall.utils.platform_paths import platform_cache_root
from albuswall.utils.xdg_thumbnail import xdg_thumbnails_root

_APP_DIR = "albuswall"
_APP_THUMB_DIR = "thumbnails"
_ENV_OVERRIDE = "ALBUSWALL_THUMBNAIL_ROOT"


def uses_xdg_thumbnails() -> bool:
    """当前平台是否走 XDG 缩略图协议。

    Linux/BSD/Solaris 等为 True；Darwin 和 NT 为 False。
    这是 bootstrap 层做平台判断的唯一入口。
    """
    if os.name != "posix":
        return False
    if sys.platform == "darwin":
        return False
    return True


def app_cache_root() -> Path:
    """软件自己的缓存根（非 XDG 平台使用）。

    与 ``bootstrap.load_session_config`` 里 ``.session`` 的位置同源。
    """
    return platform_cache_root() / _APP_DIR


def thumbnail_root() -> Path:
    """返回当前平台下应使用的缩略图根目录。

    - Linux/BSD：``$XDG_CACHE_HOME/thumbnails`` 或 ``~/.cache/thumbnails``
    - macOS/Windows：``{platform_cache_root}/albuswall/thumbnails``

    只做路径计算，不创建目录。环境变量 ``ALBUSWALL_THUMBNAIL_ROOT``
    可强制覆盖，便于测试与自定义部署。
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()

    if uses_xdg_thumbnails():
        return xdg_thumbnails_root()

    return app_cache_root() / _APP_THUMB_DIR
