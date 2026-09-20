#
""""""

from pathlib import Path

CURRENT_PATH = Path(__file__).parent

# ========== 别名 → 相对路径（文件或文件夹均可） ==========
PATH_ALIASES = {
    "theme": "themes/default",                     # 文件夹
    "sql": "sql",                                  # 文件夹
    "qss": "themes/default/main_window.qss",       # 文件
    "schema": "sql/media_library_schema.sql",       # 文件
    # 可继续添加任意文件或文件夹
}

# =====================================================

def _check_safety(path: Path) -> Path:
    """确保路径在 CURRENT_PATH 内，防止路径穿越"""
    current = CURRENT_PATH.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(current):
        raise ValueError(f"Path traversal detected: {resolved}")
    return resolved

def get(alias: str, as_folder: bool | None = None) -> Path:
    """
    根据别名获取资源路径（文件或文件夹）。

    Args:
        alias: 逻辑别名（必须在 PATH_ALIASES 中定义）
        as_folder: 若为 True，强制要求路径是文件夹；若为 False，强制要求是文件；
                   若为 None（默认），不校验类型，只检查存在性。

    Returns:
        Path 对象

    Raises:
        KeyError: 别名未定义
        FileNotFoundError: 路径不存在
        ValueError: 路径不安全或类型不匹配
    """
    if alias not in PATH_ALIASES:
        raise KeyError(f"Unknown alias: {alias}")

    target = (CURRENT_PATH / PATH_ALIASES[alias]).resolve()
    target = _check_safety(target)

    if not target.exists():
        raise FileNotFoundError(f"Path not found: {target}")

    if as_folder is True and not target.is_dir():
        raise ValueError(f"Expected folder but got file: {target}")
    if as_folder is False and not target.is_file():
        raise ValueError(f"Expected file but got folder: {target}")

    return target
