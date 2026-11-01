# 
"""跨平台应用路径。所有平台判断集中在此。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def platform_cache_root() -> Path:
    """应用私有缓存根目录。

    * Linux / BSD : $XDG_CACHE_HOME 或 ~/.cache
    * macOS       : ~/Library/Caches
    * Windows     : %LOCALAPPDATA%
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    raw = os.environ.get("XDG_CACHE_HOME")
    return Path(raw) if raw else Path.home() / ".cache"
