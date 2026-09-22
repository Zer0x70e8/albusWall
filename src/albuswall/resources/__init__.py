#
"""
albuswall.resources 包 - 提供资源路径的快捷访问。

用法：
    from albuswall.resources import theme, qss, icon_dir
    theme   -> Path('.../themes/default')          # 文件夹
    qss     -> Path('.../themes/default/main_window.qss')  # 文件
    icon_dir -> Path('.../cover')                   # 强制获取文件夹（如果别名本身是文件则会报错）
"""

from .core import get, CURRENT_PATH, PATH_ALIASES

__all__ = ["get", "CURRENT_PATH"] + list(PATH_ALIASES.keys())


def __getattr__(name):
    # 直接别名访问（文件或文件夹均可）
    if name in PATH_ALIASES:
        return get(name)

    # 以 `_dir` 结尾 -> 强制返回文件夹
    if name.endswith("_dir"):
        alias = name[:-4]  # 去掉 "_dir"
        if alias in PATH_ALIASES:
            return get(alias, as_folder=True)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    # 列出所有可用别名以及 _dir 变体，方便 IDE 自动补全
    base = sorted(globals().keys())
    aliases = list(PATH_ALIASES.keys())
    dir_variants = [f"{alias}_dir" for alias in aliases]
    return base + aliases + dir_variants


# 为静态类型检查器和 IDE 提供提示（运行时不会执行）
# noinspection PyUnreachableCode
if False:
    from pathlib import Path

    theme: Path
    ico: Path
    sql: Path
    qss: Path
    theme_dir: Path
    ico_dir: Path
    sql_dir: Path
