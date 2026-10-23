#
"""内置的设置项补丁：把 schema 的「通用形状」修成「这个项目里好用的形状」。

schema（``services/command.py``）只声明「有哪些设置」，能给的控件提示是静态
的；这里用 :mod:`~albuswall.ui.builtin.setting.overrides` 把几处明显别扭的
项改成真正可用的交互：

* ``appearance.theme`` —— 自由文本框 → **磁盘上真实存在的主题**的下拉框，
  选中后同时写 ``[ui]theme`` 与 ``[path]theme]``（样式表路径靠后者解析，
  见 ``theme_source.py`` 的说明）；
* ``appearance.theme_details.*``（``theme_path`` / ``style_sheet`` /
  ``icon_dir``）加一句「选主题会自动填」的说明；
* 一堆「改了要重启」的项（``advanced.runtime.*`` / ``advanced.files.*`` /
  ``advanced.paths.*`` / 日志级别、缩略图格式…）加 ``hint``；
* 往 ``appearance.theme_details`` 里插一条 **Edit** 动作行
  （:mod:`~albuswall.ui.builtin.setting.extras` 注册，按钮暂时没接线）。

界面上的分组顺序（主题 → 窗口标题 → 主题详情 → 缺失补全）是**登记**
出来的：``services/command.py`` 给每个叶子 / 子分组写了 ``order``，
设置页的 ``layout.py`` 按它混排。本文件不硬编码任何布局。

本模块导入即登记，``main.py`` 会 ``import`` 它一次。业务/插件想改设置页也
照这个文件写：调用 ``item_patch`` / ``group_patch`` / ``extra`` 即可，不需要
动 services。所有展示文字都写成英文（``description`` / ``label`` / ``hint``
会原样出现在界面上）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .extras import ExtraSpec, register_extra
from .overrides import UNSET, item_patch
from .theme_source import discover_themes, find_theme, theme_choices

__all__ = ["apply_theme"]

#: 改完需要重启才生效的项
_RESTART_HINT = "Restart required"


# --------------------------------------------------------------------------- #
# UI 主题
# --------------------------------------------------------------------------- #
def apply_theme(name: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """把「选中的主题名」翻译成要写回的配置节点。

    ``[path]theme`` 是真正的开关（``style_sheet`` 默认值
    ``${path:theme}/${files:qss}`` 由它解析），``[ui]theme`` 只是名字，
    两者要一起写才不会「名字变了样式没变」。

    主题目录找不到时只写名字，并把 ``theme_path`` 留空 —— 让解析退回
    用户自己配置的路径，而不是把错误路径覆盖进去。
    """
    info = find_theme(str(name))
    if info is None:
        return {"appearance.theme": name}

    mapping: dict[str, Any] = {
        "appearance.theme": info.name,
        # 配置项的 key 是「分组 qualname + 叶子名」，主题详情是子分组
        "appearance.theme_details.theme_path": str(info.path),
    }
    qss = info.qss
    if qss is not None:
        # 显式给出样式表：即便 files.qss 被改过，也不会解析到不存在的文件
        mapping["appearance.theme_details.style_sheet"] = str(qss)
    return mapping


@item_patch(
    "appearance.theme",
    widget="choice",
    choices_provider=theme_choices,
    hint=_RESTART_HINT,
    description=(
        "Pick one of the themes found on disk. Themes are directories that "
        "contain a QSS file: the bundled ones live in resources/themes/, "
        "user themes go either next to the current theme directory or under "
        "<config dir>/themes."
    ),
)
def _patch_theme(name: str, item: dict[str, Any]) -> dict[str, Any]:
    return apply_theme(name, item)


@item_patch(
    "appearance.theme_details.theme_path",
    description=(
        "Directory of the selected theme. Picking a theme above fills this "
        "in automatically; the stylesheet and the icon directory are derived "
        "from it."
    ),
)
def _patch_theme_path(value: Any, item: dict[str, Any]) -> None:
    return None


@item_patch(
    "appearance.theme_details.style_sheet",
    hint=_RESTART_HINT,
    description=(
        "Stylesheet file of the selected theme. Empty = derived from the "
        "theme directory (${path:theme}/${files:qss})."
    ),
)
def _patch_style_sheet(value: Any, item: dict[str, Any]) -> None:
    return None


@item_patch(
    "appearance.theme_details.icon_dir",
    description=(
        "Icon directory of the selected theme. Empty = icons/ cover/ imgs/ "
        "assets/ next to the stylesheet."
    ),
)
def _patch_icon_dir(value: Any, item: dict[str, Any]) -> None:
    return None


@item_patch(
    "appearance.window_title",
    description="Overrides the window title. Empty = follow the album name.",
)
def _patch_window_title(value: Any, item: dict[str, Any]) -> None:
    return None


# --------------------------------------------------------------------------- #
# 主题详情里的动作行（暂无实现）
# --------------------------------------------------------------------------- #
#: 主题详情分组里「编辑」动作行的注册 id
THEME_EDIT_EXTRA_ID = "theme_details.edit"


def register_theme_edit_action() -> ExtraSpec:
    """登记 ``Theme details`` 分组里的 **Edit** 动作行。

    这一行没有配置节点，也不该写死在 ``main.py``：它是一条
    :class:`~albuswall.ui.builtin.setting.extras.ExtraSpec`，和配置项一样
    由注册表驱动、由 ``layout.py`` 排序。按钮目前**没有接线**（点了不做任何
    事），留给以后的「主题编辑器」。
    """
    return register_extra(
        ExtraSpec(
            id=THEME_EDIT_EXTRA_ID,
            group="appearance.theme_details",
            title="Edit",
            order=40,
            description=(
                "Reserved action for a future theme editor: it will open the "
                "stylesheet and the icons of the selected theme for editing."
            ),
            button="Edit",
        ),
        replace=True,
    )


register_theme_edit_action()


# --------------------------------------------------------------------------- #
# 需要重启的项
# --------------------------------------------------------------------------- #
def _mark_restart(key: str, description: str | None = None):
    @item_patch(key, hint=_RESTART_HINT, description=description or UNSET)
    def _noop(value: Any, item: dict[str, Any]) -> None:
        return None


for _key in (
        "advanced.runtime.ui",
        "advanced.runtime.no_builtin",
        "advanced.runtime.disable_type_check",
        "advanced.files.qss",
        "advanced.files.preference",
        "advanced.files.window_state",
        "advanced.files.db",
        "advanced.files.log",
        "advanced.paths.config",
        "advanced.paths.data",
        "advanced.paths.cache",
        "advanced.paths.temp",
        "advanced.logging.level",
        "advanced.logging.config",
        "advanced.ipc.enabled",
        "advanced.ipc.event_bridge",
        "advanced.database.must_exist",
        "advanced.thumbnail_tuning.image_format",
        "advanced.thumbnail_tuning.quality",
):
    _mark_restart(_key)


# --------------------------------------------------------------------------- #
# 自检辅助（调试用；不参与 UI）
# --------------------------------------------------------------------------- #
def describe_themes() -> list[str]:
    """当前能扫到的主题（``主题名 -> 目录``），排障用。"""
    return [f"{info.name} -> {info.path}" for info in discover_themes()]
