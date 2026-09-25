#
"""
AlbusWall - Hyprland Image Gallery
Your wall of memories, frame by frame.
A clean grid gallery for your ricing shots.

Additional subtitles:
...
"""
from importlib.metadata import version, PackageNotFoundError

__title__ = "albuswall"
__author__ = "Zer0x70e8"
__version__ = "0.1.3.dev0"
try:
    __version__ = version("albuswall")
except PackageNotFoundError:
    # noinspection broad-exception
    try:
        import tomllib
        from pathlib import Path

        _pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with _pyproject.open("rb") as f:
            __version__ = tomllib.load(f)["project"]["version"]
    except Exception:
        pass
_parts = __version__.split(".")
__version_info__ = (
    int(_parts[0]),
    int(_parts[1]),
    int(_parts[2].split("+")[0].split("a")[0].split("b")[0].split("rc")[0] or 0),
    "",
    "",
)

__minimum_python_version__ = (3, 11)
__maximum_python_version__ = (3, 14)
