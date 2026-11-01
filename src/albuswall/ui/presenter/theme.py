#
"""窗口主题：``[ui] theme`` 的声明 + QSS 解析。

只描述「主题是什么、QSS 从哪儿来」，**不碰 Qt** —— 应用样式表是
:class:`~albuswall.ui.presenter.main.MainPresenter` 的事，这样本模块在无
Qt 的单元测试里也能直接用。

取值来源（优先级从高到低）
==========================

1. ``[ui] style_sheet`` —— 显式 QSS 文件路径（``~`` 会展开）。给「我就要用
   这个文件」的场合，不再看 ``theme``；
2. ``[ui] theme`` —— 主题名，对应内置目录 ``resources/themes/<name>/``；
3. 内置 ``default`` 主题 —— 配置里写的名字不存在时回退（**记 WARNING**，
   回退不静默）；
4. 空样式表 —— 连内置主题都读不到（打包事故 / 资源缺失）时仍然启动，
   只是窗口不套皮。

内置主题目录形如::

    resources/themes/<name>/
    ├─ main_window.qss     # 入口样式表（THEME_QSS_NAME）
    └─ icons/…             # 主题自带图标

说明：``[path] theme`` / ``[files] qss``（随包 ``conf.ini`` 里的
「用户主题自动补全」）目前**未实现** —— 那套复制逻辑属于设置页，等它落地时
在 :func:`resolve_theme` 前面加一档「用户目录优先」即可，调用方不用改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError

__all__ = [
    "DEFAULT_THEME",
    "ICON_DIR_NAME",
    "THEME_QSS_NAME",
    "Theme",
    "ThemeConfig",
    "available_themes",
    "configured_style_sheet",
    "configured_theme",
    "resolve_theme",
    "themes_root",
]

logger = logging.getLogger(__package__)

#: 配置缺值 / 未知主题名时的回退主题。
DEFAULT_THEME = "default"

#: 每个主题目录里的入口样式表文件名。
THEME_QSS_NAME = "main_window.qss"

#: 主题目录里的图标子目录名（与入口样式表同级）。
ICON_DIR_NAME = "icons"

#: QSS 的来源标记。
ThemeSource = Literal["config", "bundled", "empty"]


# noinspection bad-assignment
@configclass("ui", "main")
class ThemeConfig:
    """``[ui]`` 段里与主题相关的键（与 :class:`albuswall.ui.application.UIConf`
    同一个 section，字段互不重叠）。

    ``albuswall.ui`` 在插件 setup 阶段（``setup_config`` + ``load()`` 之后）
    才被 import，``configclass`` 对已构造的 ``Configue`` 会在装饰当下立即注入
    类型与默认值（``inject_raw(overwrite=False)``），所以这里可以直接写默认值；
    文件里写了的值照样赢。

    ``style_sheet`` 为空表示「按 ``theme`` 找内置主题」；非空时按
    :func:`resolve_theme` 的优先级处理。
    """

    theme: str = DEFAULT_THEME
    style_sheet: str = ""


@dataclass(frozen=True)
class Theme:
    """一份已解析的主题。

    ``name`` 是**实际生效**的主题名（可能因为回退而不同于请求的名字）；
    ``source`` 说明 QSS 从哪儿来：

    * ``"config"`` —— ``[ui] style_sheet`` 显式指定的文件；
    * ``"bundled"`` —— ``resources/themes/<name>/`` 里的内置样式表；
    * ``"empty"`` —— 没找到任何样式表，``style_sheet`` 为空串。
    """

    name: str
    source: ThemeSource
    qss_path: Path | None = None
    style_sheet: str = ""

    @property
    def is_empty(self) -> bool:
        """是否没有可用样式表（空串 / 纯空白都算空）。"""
        return not self.style_sheet.strip()

    @property
    def root(self) -> Path | None:
        """主题目录（入口样式表所在目录）；没有样式表文件时为 ``None``。"""
        return None if self.qss_path is None else self.qss_path.parent

    @property
    def icons_dir(self) -> Path | None:
        """主题的图标目录（``<theme>/icons``）；不存在时为 ``None``。"""
        root = self.root
        if root is None:
            return None
        directory = root / ICON_DIR_NAME
        return directory if directory.is_dir() else None

    def icon(self, name: str) -> Path | None:
        """取主题图标路径。

        :param name: 图标名，可带或不带扩展名（``"album_button"`` /
            ``"album_button.svg"``）。
        :return: 存在的图标文件路径；主题里没有这张图时返回 ``None``，
            调用方据此回退到纯文字按钮。
        """
        directory = self.icons_dir
        if directory is None or not name:
            return None
        candidates = (name,) if Path(name).suffix else (f"{name}.svg", name)
        for candidate in candidates:
            path = directory / candidate
            if path.is_file():
                return path
        return None


# ===========================================================================
# 配置读取
# ===========================================================================

def _configured() -> tuple[str, str]:
    """``(theme_name, style_sheet)``；配置系统不可用时返回默认值与空串。

    与 ``service/thumbnail/config.py::thumbnail_config`` 同一策略：只兜
    ``ConfigError`` / ``AttributeError``（``ConfigMissingError`` 同时是两者），
    编程错误照常往上抛。
    """
    try:
        config = ThemeConfig()
        theme = str(config.theme or "").strip() or DEFAULT_THEME
        style_sheet = str(config.style_sheet or "").strip()
    except (ConfigError, AttributeError) as exc:
        logger.debug(
            "theme: config unavailable (%s: %s); using declared defaults",
            type(exc).__name__, exc,
        )
        return DEFAULT_THEME, ""
    return theme, style_sheet


def configured_theme() -> str:
    """``[ui] theme``（空白 / 缺值 → :data:`DEFAULT_THEME`）。"""
    return _configured()[0]


def configured_style_sheet() -> str:
    """``[ui] style_sheet`` 的原始字符串（缺值 → 空串）。"""
    return _configured()[1]


# ===========================================================================
# 内置主题目录
# ===========================================================================

def themes_root() -> Path | None:
    """内置主题根目录 ``resources/themes/``；资源缺失时返回 ``None``。"""
    try:
        from albuswall import resources

        root = resources.theme.parent
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logger.debug("theme: bundled theme dir unavailable (%s)", exc)
        return None
    return root if root.is_dir() else None


def available_themes() -> tuple[str, ...]:
    """内置主题名（有入口样式表的目录），按名字排序。"""
    root = themes_root()
    if root is None:
        return ()
    return tuple(sorted(
        entry.name
        for entry in root.iterdir()
        if (entry / THEME_QSS_NAME).is_file()
    ))


# ===========================================================================
# 解析
# ===========================================================================

def resolve_theme(name: str | None = None) -> Theme:
    """解析主题：``name`` 为空时读 ``[ui]`` 配置。

    Args:
        name: 显式主题名；显式给名字时**忽略** ``[ui] style_sheet``
            （调用方已经指名道姓，配置里的路径覆盖只会造成意外）。
            ``None`` / 空白 → 先看 ``[ui] style_sheet``，再按 ``[ui] theme``。

    Returns:
        解析结果。``Theme.name`` 是实际生效的名字：请求了不存在的主题时，
        这里回退到 ``default`` 并记 WARNING，调用方据此决定要不要回写配置。
    """
    configured_name, override = _configured()
    explicit = name is not None and str(name).strip() != ""
    requested = str(name).strip() if explicit else configured_name

    if not explicit and override:
        theme = _read_qss(requested, Path(override).expanduser(), "config")
        if theme is not None:
            return theme
        logger.warning(
            "theme: [ui] style_sheet %r not readable; "
            "falling back to theme %r", override, requested)

    theme = _bundled(requested)
    if theme is not None:
        return theme

    if requested != DEFAULT_THEME:
        logger.warning(
            "theme: %r has no bundled %s (available: %s); "
            "falling back to %r",
            requested, THEME_QSS_NAME, ", ".join(available_themes()) or "none",
            DEFAULT_THEME,
        )
        theme = _bundled(DEFAULT_THEME)
        if theme is not None:
            return theme

    logger.warning(
        "theme: no stylesheet found for %r; window stays unstyled", requested)
    return Theme(name=requested, source="empty")


def _bundled(name: str) -> Theme | None:
    root = themes_root()
    if root is None:
        return None
    return _read_qss(name, root / name / THEME_QSS_NAME, "bundled")


def _read_qss(name: str, path: Path, source: ThemeSource) -> Theme | None:
    """读一个 QSS 文件；不存在 / 读失败返回 ``None``（由调用方回退）。"""
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("theme: cannot read %s: %s", path, exc)
        return None
    return Theme(name=name, source=source, qss_path=path, style_sheet=text)
