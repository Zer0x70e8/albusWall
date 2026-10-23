#
"""主题发现：把「磁盘上真实存在的主题」变成设置页里的候选项。

``[ui]theme`` 只是一个名字，真正决定外观的是 ``[path]theme`` —— 指向
**选中主题的目录**，``[ui]style_sheet`` 的默认值就是
``${path:theme}/${files:qss}``（图标目录也由样式表同级推出，见
``builtin/config/icons.py``）。

所以「选主题」这件事必须同时写这两个节点，否则会出现「名字改了、样式没变」
或者 ``${path:theme}/main_window.qss`` 解析到字面量 ``None/`` 的怪现象。
本模块只负责**发现**（纯路径运算，不碰配置对象），写入由
``builtin_patches.py`` 的写回钩子完成。

主题候选来源（按顺序，后者去重）::

    1. 当前 ``[path]theme`` 的同级目录（用户自己放的主题）
    2. <config>/themes、<data>/themes（用户的主题目录约定）
    3. 内置 resources/themes/
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from albuswall.log import getLogger

__all__ = [
    "QSS_FILE_CANDIDATES",
    "ThemeInfo",
    "discover_themes",
    "find_theme",
    "theme_choices",
    "theme_roots",
]

_logger = getLogger(__name__)

#: 一个目录要算作主题，至少得有一个这样的 QSS 文件
QSS_FILE_CANDIDATES = ("main_window.qss", "qss.qss", "style.qss")


@dataclass(frozen=True)
class ThemeInfo:
    """一个可用的主题。"""

    name: str
    path: Path
    builtin: bool = False

    @property
    def qss(self) -> Path | None:
        """主题里的样式表文件（没有就是 ``None``）。"""
        for file_name in QSS_FILE_CANDIDATES:
            candidate = self.path / file_name
            if candidate.is_file():
                return candidate
        return None

    @property
    def display(self) -> str:
        """下拉框里的显示文本（英文）。"""
        return f"{self.name} (built-in)" if self.builtin else self.name


# --------------------------------------------------------------------------- #
# 路径候选
# --------------------------------------------------------------------------- #
def _bundled_theme_root() -> Path | None:
    try:
        from albuswall.resources import CURRENT_PATH

        return Path(CURRENT_PATH) / "themes"
    except Exception:  # noqa: BLE001  资源目录缺失不该炸设置页
        return None


def _user_theme_roots() -> list[Path]:
    """用户可能放主题的目录（配置目录 / 数据目录下的 ``themes``）。"""
    roots: list[Path] = []
    try:
        from albuswall import __author__, __title__
        from albuswall.configue.utils import (
            get_user_config_dir,
            get_user_data_dir,
        )

        roots.append(
            Path(get_user_config_dir(__title__, __author__)) / "themes"
        )
        roots.append(
            Path(get_user_data_dir(__title__, __author__)) / "themes"
        )
    except Exception:  # noqa: BLE001  平台目录推导失败时退回内置主题
        _logger.debug("cannot resolve user theme roots", exc_info=True)
    return roots


def _current_theme_dir() -> Path | None:
    """当前 ``[path]theme``（选中的主题目录）。"""
    try:
        from albuswall.configue.declaration import get_config

        resolver = get_config().resolver
        if ("path", "theme") not in resolver:
            return None
        raw = resolver.get_typed("path", "theme")
    except Exception:  # noqa: BLE001  配置未就绪时没有「当前主题」
        return None
    if not raw:
        return None
    return Path(str(raw))


def theme_roots() -> list[tuple[Path, bool]]:
    """``(目录, 是否内置)`` 列表：要扫描哪些目录找主题。"""
    roots: list[tuple[Path, bool]] = []

    current = _current_theme_dir()
    if current is not None:
        # 当前主题的同级目录 = 用户自己放主题最自然的位置
        roots.append((current.parent, False))

    for root in _user_theme_roots():
        roots.append((root, False))

    bundled = _bundled_theme_root()
    if bundled is not None:
        roots.append((bundled, True))

    deduped: list[tuple[Path, bool]] = []
    seen: set[Path] = set()
    for root, builtin in roots:
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen:
            continue
        seen.add(key)
        deduped.append((root, builtin))
    return deduped


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def discover_themes() -> list[ThemeInfo]:
    """扫描所有候选目录，返回可用主题（同名时用户主题优先于内置）。"""
    found: dict[str, ThemeInfo] = {}

    for root, builtin in theme_roots():
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            _logger.debug("cannot list theme root %s", root, exc_info=True)
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            info = ThemeInfo(name=entry.name, path=entry, builtin=builtin)
            if info.qss is None:
                # 没有样式表的目录不算主题
                continue
            existing = found.get(info.name)
            if existing is not None and existing.builtin is False \
                    and builtin is True:
                continue  # 用户主题优先
            found[info.name] = info

    return sorted(found.values(), key=lambda info: info.name.casefold())


def find_theme(name: str) -> ThemeInfo | None:
    """按名字找主题（找不到返回 ``None``）。"""
    if not name:
        return None
    for info in discover_themes():
        if info.name == name:
            return info
    return None


def theme_choices() -> list[str]:
    """下拉框候选：主题名列表（动态，每次建控件时重新扫）。"""
    names = [info.name for info in discover_themes()]
    if not names:
        # 一个都没扫到时至少把「当前值」保住，别让下拉框空着
        current = _current_theme_dir()
        if current is not None:
            names.append(current.name)
    return names
