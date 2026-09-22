#
"""Cross-platform default path."""

import importlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

__all__ = [
    "get_user_config_dir",
    "get_temp_dir",
    "get_cache_dir",
    "get_user_data_dir"
]


def get_user_config_dir(app_name: str, app_author: Optional[str] = None) -> Path:
    """
    Return the user configuration directory.
    Follows platform conventions:
    - Windows: %APPDATA%/<author>/<app> or %APPDATA%/<app>
    - macOS:   ~/Library/Application Support/<app>
    - Linux:   ~/.config/<app>
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        if app_author:
            return Path(base) / app_author / app_name
        return Path(base) / app_name
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    else:  # Linux and other Unix
        xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return Path(xdg_config) / app_name


def get_user_data_dir(app_name: str, app_author: Optional[str] = None) -> Path:
    """
    Return the user data directory.
    Follows platform conventions:
    - Windows: %APPDATA%/<author>/<app> or %APPDATA%/<app>
    - macOS:   ~/Library/Application Support/<app>
    - Linux:   $XDG_DATA_HOME/<app> (default ~/.local/share/<app>)
    Note: On Windows/macOS, config and data often share the same directory.
    """
    if sys.platform == "win32":
        # Windows: config and data share %APPDATA%
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        if app_author:
            return Path(base) / app_author / app_name
        return Path(base) / app_name
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return Path(xdg_data) / app_name


def get_cache_dir(app_name: str, app_author: Optional[str] = None) -> Path:
    """Return the persistent cache directory."""
    # Prefer platformdirs, fallback to standard library
    try:
        mod = importlib.import_module("platformdirs")
        user_cache_dir = mod.user_cache_dir
        persistent = Path(user_cache_dir(app_name, app_author))
    except ImportError:
        persistent = _fallback_cache_dir(app_name, app_author)
    return persistent


def get_temp_dir(app_name: str) -> Path:
    """Return the temporary cache directory (system tmp + app subfolder)."""
    temporary = Path(tempfile.gettempdir()) / app_name
    temporary.mkdir(parents=True, exist_ok=True)
    return temporary


def _fallback_cache_dir(app_name: str, app_author: Optional[str] = None) -> Path:
    """Fallback implementation following platform conventions."""
    home = Path.home()

    if sys.platform == "win32":
        # Windows: %LOCALAPPDATA%\<author>\<app>\Cache
        base = os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")
        if app_author:
            return Path(base) / app_author / app_name / "Cache"
        return Path(base) / app_name / "Cache"

    elif sys.platform == "darwin":
        # macOS: ~/Library/Caches/<app>
        return home / "Library" / "Caches" / app_name

    else:
        # Linux: follows XDG_CACHE_HOME, default ~/.cache
        base = os.environ.get("XDG_CACHE_HOME", home / ".cache")
        return Path(base) / app_name
