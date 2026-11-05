#
"""窗口主题：``[ui]`` 的声明 + QSS / 图标路径的**配置组合**。

只描述「主题是什么、文件从哪儿来」，**不碰 Qt** —— 应用样式表是
:class:`~albuswall.ui.presenter.main.MainPresenter` 的事，这样本模块在无
Qt 的单元测试里也能直接用。

路径全部来自配置器的 ``${...}`` 插值
====================================

本模块**不**自己拼文件名 / 目录名，而是读 ``[ui]`` 里组合好的键；随包
``resources/config/conf.ini`` 是同一份默认值的文档::

    [path]
    theme = ${path:config}/theme                 # 用户主题根目录
    icons = ${theme}/${ui:theme}/icons           # 当前主题的图标目录

    [files]
    main_window_qss = main_window.qss
    album_button    = album_button.svg           # + 其余图标文件名

    [ui]
    theme        = default
    style_sheet  = ${path:theme}/${ui:theme}/${files:main_window_qss}
    album_button = ${path:icons}/${files:album_button}
    …

于是「切主题」只写一个键：``[ui] theme``。``${ui:theme}`` 的展开缓存会随之
失效（:mod:`albuswall.configue.resolver` 记录 ``${...}`` 依赖），所有派生路径
自动指向新主题，UI 侧不需要任何重新拼路径的逻辑。

解析顺序（优先级从高到低）
==========================

1. ``[ui] style_sheet`` —— 展开后的绝对路径；文件存在就直接用（``source
   = "config"``）。它默认指向用户主题目录，用户想覆盖内置主题时把文件放到
   ``${path:theme}/<name>/`` 下即可，不必改配置；
2. 随包主题 ``resources/themes/<name>/``（``source = "bundled"``）；
3. 随包 ``default`` 主题 —— 请求的名字不存在时回退（**记 WARNING**，
   回退不静默）；
4. 空样式表 —— 连内置主题都读不到（打包事故 / 资源缺失）时仍然启动，
   只是窗口不套皮。

图标同理，三级：``[ui] <icon key>`` 组合出的文件（用户主题目录，存在才算）→
当前主题的 ``icons/`` 目录（``[path] icons`` / ``<themes root>/<name>/icons/``）→
随包 ``default`` 主题的 ``icons/``（用户主题只换了几张图时其余图标仍有默认外观）。
三处都没有时 :meth:`Theme.icon` 返回 ``None``，调用方回退到纯文字。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from albuswall.common.constants.ui import (
    DEFAULT_THEME,
    ICON_DIR_NAME,
    ICON_KEYS,
    ICON_SUFFIX,
    THEME_QSS_NAME,
)
from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError

__all__ = [
    "DEFAULT_THEME",
    "ICON_DIR_NAME",
    "ICON_KEYS",
    "THEME_QSS_NAME",
    "Theme",
    "ThemeConfig",
    "ThemeLayout",
    "available_themes",
    "configured_style_sheet",
    "configured_theme",
    "configured_theme_qss",
    "resolve_theme",
    "themes_root",
]

logger = logging.getLogger(__package__)

#: QSS 的来源标记。
ThemeSource = Literal["config", "bundled", "empty"]

#: ``[ui]`` 里与主题相关的默认组合式。
#:
#: 与随包 ``conf.ini`` **逐字一致**：用户配置文件没写这几个键时，声明期的
#: 默认值就是这一份，展开后与文件里写的组合式完全等价。
STYLE_SHEET_EXPR = "${path:theme}/${ui:theme}/${files:main_window_qss}"
ICON_EXPR = "${path:icons}/${files:%s}"


# noinspection bad-assignment
@configclass("ui", "main")
class ThemeConfig:
    """``[ui]`` 段里与主题相关的键。

    ``albuswall.ui`` 在插件 setup 阶段（``setup_config`` + ``load()`` 之后）才被
    import，``configclass`` 对已构造的 ``Configue`` 会在装饰当下立即注入类型与
    默认值（``inject_raw(overwrite=False)``），所以这里可以直接写默认值；
    文件里写了的值照样赢。

    ``theme`` 与 :class:`albuswall.configue.bootstrap.UiSelection` 是同一个节点的
    两份声明（类型与默认值必须一致）：后者保证 ``load()`` 期间
    ``${ui:theme}`` 就能展开，前者跟着 UI 的其余键一起声明。

    其余字段都是**组合式**而不是路径字面量：``${...}`` 由配置器在读取时展开，
    因此改 ``[path]`` / ``[files]`` / ``[ui] theme`` 任意一处，这里的结果都会跟着
    变（含运行期 ``MainPresenter.set_theme`` 的回写）。
    """

    theme: str = DEFAULT_THEME
    style_sheet: str = STYLE_SHEET_EXPR
    app_icon: str = ICON_EXPR % "app_icon"
    album_button: str = ICON_EXPR % "album_button"
    search_button: str = ICON_EXPR % "search_button"
    more_button: str = ICON_EXPR % "more_button"
    favorite_icon: str = ICON_EXPR % "favorite_icon"


@dataclass(frozen=True)
class ThemeLayout:
    """一次读出的「主题布局」：``[ui]`` + ``[path]`` + ``[files]`` 的合并视图。

    配置系统不可用（单测单独 import UI / 没构造 ``Configue``）时，所有字段退回
    公共常量（:mod:`albuswall.common.constants.ui`），调用方无需判断。

    Attributes:
        theme: ``[ui] theme``：生效主题名。
        style_sheet: ``[ui] style_sheet`` 展开后的用户主题 QSS 路径；空白 /
            缺值 / 未展开的模板为 ``None``。
        icons: ``[ui] <icon key>`` 展开后的候选路径（**未校验存在性**）。
        theme_root: ``[path] theme``：用户主题根目录（``<root>/<name>/…``）。
        icons_dir: ``[path] icons``：**配置里那个主题**的图标目录
            （默认 ``<theme root>/<name>/icons``，含 ``${ui:theme}``）。
        qss_name: ``[files] main_window_qss``：随包主题里的入口样式表文件名。
    """

    theme: str = DEFAULT_THEME
    style_sheet: Path | None = None
    icons: Mapping[str, Path] = field(default_factory=dict)
    theme_root: Path | None = None
    icons_dir: Path | None = None
    qss_name: str = THEME_QSS_NAME

    def user_qss(self, name: str) -> Path | None:
        """用户主题目录里 ``name`` 的入口样式表（目录未知时 ``None``）。"""
        if self.theme_root is None or not name:
            return None
        return self.theme_root / name / self.qss_name

    def user_icons(self, name: str) -> Path | None:
        """用户主题目录里 ``name`` 的图标目录（与 :meth:`user_qss` 同一布局）。"""
        root = self.user_qss(name)
        return None if root is None else root.parent / ICON_DIR_NAME


@dataclass(frozen=True)
class Theme:
    """一份已解析的主题。

    ``name`` 是**实际生效**的主题名（可能因为回退而不同于请求的名字）；
    ``source`` 说明 QSS 从哪儿来：

    * ``"config"`` —— ``[ui] style_sheet``（用户主题目录）里的文件；
    * ``"bundled"`` —— ``resources/themes/<name>/`` 里的内置样式表；
    * ``"empty"`` —— 没找到任何样式表，``style_sheet`` 为空串。

    ``icons`` 是 ``[ui]`` 组合出来且**确实存在**的图标文件（逻辑名 → 路径）；
    ``icons_dir`` / ``fallback_icons_dir`` 是目录级兜底：前者是用户主题（或随包
    主题）的 ``icons/``，后者是随包主题的 ``icons/``——用户主题只放了两三个图标
    时，其余图标仍落到随包主题上，而不是整片按钮退回文字。三处都没有时
    :meth:`icon` 返回 ``None``，调用方回退文字。
    """

    name: str
    source: ThemeSource
    qss_path: Path | None = None
    style_sheet: str = ""
    icons: Mapping[str, Path] = field(default_factory=dict)
    icons_dir: Path | None = None
    fallback_icons_dir: Path | None = None

    @property
    def is_empty(self) -> bool:
        """是否没有可用样式表（空串 / 纯空白都算空）。"""
        return not self.style_sheet.strip()

    @property
    def root(self) -> Path | None:
        """主题目录（入口样式表所在目录）；没有样式表文件时为 ``None``。"""
        return None if self.qss_path is None else self.qss_path.parent

    def icon(self, name: str) -> Path | None:
        """取主题图标路径。

        先查 ``[ui]`` 里为该逻辑名组合出的文件（用户主题目录 / 配置显式指定的
        路径），再在 ``icons_dir`` / ``fallback_icons_dir`` 里按名字找：

        :param name: 图标逻辑名，即 ``[ui]`` / ``[files]`` 的键名
            （``"album_button"``），也接受带扩展名的文件名
            （``"album_button.svg"``）。
        :return: 存在的图标文件路径；没有这张图时返回 ``None``，调用方据此回退
            到纯文字按钮。
        """
        configured = self.icons.get(name)
        if configured is not None:
            return configured
        if not name:
            return None
        candidates = (
            (name,) if Path(name).suffix
            else (f"{name}{ICON_SUFFIX}", name)
        )
        for directory in (self.icons_dir, self.fallback_icons_dir):
            if directory is None:
                continue
            for candidate in candidates:
                path = directory / candidate
                if path.is_file():
                    return path
        return None


# ===========================================================================
# 配置读取（``${...}`` 在这里展开）
# ===========================================================================

def _to_path(raw: object) -> Path | None:
    """配置值 → ``Path``；空白 / 含未展开 ``${...}`` 的值视为「没配置」。

    ``${...}`` 残留意味着配置骨架缺了 ``[path]`` / ``[files]`` 的某个节点
    （例如只 register 了 ``ThemeConfig`` 却没有 ``setup_config``）：这时把它当
    路径用只会得到一个永远不存在的文件名，不如直接当作未配置。
    """
    text = str(raw or "").strip()
    if not text or "${" in text:
        return None
    return Path(text).expanduser()


def _layout() -> ThemeLayout:
    """读 ``[ui]`` / ``[path]`` / ``[files]``；配置不可用时返回常量兜底。

    只兜 ``ConfigError`` / ``AttributeError``（``ConfigMissingError`` 同时是
    两者）与插值时缺节点抛的 ``KeyError``；编程错误照常往上抛。
    """
    try:
        config = ThemeConfig()
        theme = str(config.theme or "").strip() or DEFAULT_THEME
        style_sheet = _to_path(config.style_sheet)
        icons = {}
        for key in ICON_KEYS:
            path = _to_path(getattr(config, key, ""))
            if path is not None:
                icons[key] = path
    except (ConfigError, AttributeError, KeyError) as exc:
        logger.debug(
            "theme: config unavailable (%s: %s); using declared defaults",
            type(exc).__name__, exc,
        )
        return ThemeLayout()

    qss_name = THEME_QSS_NAME
    theme_root: Path | None = None
    icons_dir: Path | None = None
    try:
        from albuswall.configue.bootstrap import Files, Paths

        theme_root = _to_path(Paths().theme)
        icons_dir = _to_path(Paths().icons)
        qss_name = str(Files().main_window_qss or "").strip() or THEME_QSS_NAME
    except (ConfigError, AttributeError, KeyError) as exc:
        logger.debug(
            "theme: [path]/[files] unavailable (%s: %s); "
            "bundled lookup uses the declared layout",
            type(exc).__name__, exc,
        )

    return ThemeLayout(
        theme=theme,
        style_sheet=style_sheet,
        icons=icons,
        theme_root=theme_root,
        icons_dir=icons_dir,
        qss_name=qss_name,
    )


def configured_theme() -> str:
    """``[ui] theme``（空白 / 缺值 → :data:`DEFAULT_THEME`）。"""
    return _layout().theme


def configured_style_sheet() -> str:
    """``[ui] style_sheet``（展开后的字符串；缺值 → 空串）。"""
    path = _layout().style_sheet
    return "" if path is None else str(path)


def configured_theme_qss(name: str | None = None) -> Path | None:
    """用户主题目录里 ``name``（默认取 ``[ui] theme``）的入口样式表路径。

    只是**推算**：文件不一定存在。给设置页 / 调试用。
    """
    layout = _layout()
    return layout.user_qss(layout.theme if name is None else name)


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


def _available_themes(layout: ThemeLayout) -> tuple[str, ...]:
    """内置主题名（有入口样式表的目录），按名字排序。"""
    root = themes_root()
    if root is None:
        return ()
    return tuple(sorted(
        entry.name
        for entry in root.iterdir()
        if (entry / layout.qss_name).is_file()
    ))


def available_themes() -> tuple[str, ...]:
    """内置主题名（``[files] main_window_qss`` 命中的目录），按名字排序。"""
    return _available_themes(_layout())


# ===========================================================================
# 解析
# ===========================================================================

def resolve_theme(name: str | None = None) -> Theme:
    """解析主题：``name`` 为空时按 ``[ui]`` 配置。

    Args:
        name: 显式主题名；显式给名字时按**同一套目录布局**推算用户主题路径
            （``${path:theme}/<name>/${files:main_window_qss}``），不再看
            ``[ui] style_sheet`` —— 那里存的是配置里那个主题的组合式。
            ``None`` / 空白 → 用 ``[ui] style_sheet``，再回退随包主题。

    Returns:
        解析结果。``Theme.name`` 是实际生效的名字：请求了不存在的主题时，
        这里回退到 ``default`` 并记 WARNING，调用方据此决定要不要回写配置。
    """
    layout = _layout()
    explicit = name is not None and str(name).strip() != ""
    requested = str(name).strip() if explicit else layout.theme

    configured = None if explicit else layout.style_sheet
    candidate = configured if configured is not None else layout.user_qss(requested)
    theme = _read_qss(requested, candidate, "config") if candidate is not None else None
    if theme is not None:
        return _with_icons(theme, layout)

    if configured is not None:
        # 用户主题目录默认是空的（随包主题才是常态），所以只有「用户把
        # style_sheet 指到了别处」才值得 WARNING；组合式本身没命中只记 DEBUG。
        expected = layout.user_qss(requested)
        if expected is None or configured != expected:
            logger.warning(
                "theme: [ui] style_sheet %s is not readable; "
                "falling back to theme %r", configured, requested)
        else:
            logger.debug(
                "theme: user theme %r not present (%s); using the bundled one",
                requested, configured)

    theme = _bundled(requested, layout)
    if theme is not None:
        return _with_icons(theme, layout)

    if requested != DEFAULT_THEME:
        logger.warning(
            "theme: %r has no bundled %s (available: %s); "
            "falling back to %r",
            requested, layout.qss_name,
            ", ".join(_available_themes(layout)) or "none", DEFAULT_THEME,
        )
        theme = _bundled(DEFAULT_THEME, layout)
        if theme is not None:
            return _with_icons(theme, layout)

    logger.warning(
        "theme: no stylesheet found for %r; window stays unstyled", requested)
    return Theme(name=requested, source="empty")


def _bundled(name: str, layout: ThemeLayout) -> Theme | None:
    root = themes_root()
    if root is None:
        return None
    return _read_qss(name, root / name / layout.qss_name, "bundled")


def _with_icons(theme: Theme, layout: ThemeLayout) -> Theme:
    """把 ``[ui]`` 组合出的图标 + 目录兜底装到已解析的主题上。"""
    icons = _configured_icons(theme, layout)
    primary = _icons_dir(theme.name, layout)
    bundled = _bundled_icons_dir(theme.name)
    if primary is None:
        primary = bundled
    fallback = bundled if primary is not None and primary != bundled else None
    if not icons and primary is None:
        return theme
    return replace(
        theme, icons=icons, icons_dir=primary, fallback_icons_dir=fallback,
    )


def _configured_icons(theme: Theme, layout: ThemeLayout) -> dict[str, Path]:
    """``[ui]`` 里为该主题组合出且确实存在的图标文件。

    组合式里带着 ``${ui:theme}``，所以只有「生效主题名 == 配置里的主题名」时
    这些路径才属于它；显式解析成别的主题时只走目录兜底。
    """
    if not layout.icons or theme.name != layout.theme:
        return {}
    return {
        key: path for key, path in layout.icons.items() if path.is_file()
    }


def _icons_dir(name: str, layout: ThemeLayout) -> Path | None:
    """**用户主题**的图标目录（不存在则 ``None``）。

    两个候选都按「同一主题名」取：``[path] icons`` 是配置里那个主题的目录（带
    ``${ui:theme}``，所以只有名字对得上时才可用），``user_icons`` 是按布局推算
    的同名目录（显式解析成别的主题时也只有它可能命中）。
    """
    candidates = [
        layout.icons_dir if name == layout.theme else None,
        layout.user_icons(name),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def _bundled_icons_dir(name: str) -> Path | None:
    """随包主题的图标目录 ``resources/themes/<name>/icons/``。

    同名主题不存在时退到随包 ``default``：用户自建的主题通常只换样式表和一两
    张图，其余图标仍该有默认外观（与 QSS 的「未知主题回退 default」同一策略）。
    """
    root = themes_root()
    if root is None or not name:
        return None
    for candidate in (name, DEFAULT_THEME):
        directory = root / candidate / ICON_DIR_NAME
        if directory.is_dir():
            return directory
    return None


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
