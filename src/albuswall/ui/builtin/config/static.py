#
"""UI 静态配置声明。

这里的 ``ConfigField`` / ``PathField`` 是**默认值与类型的唯一来源**：
UI 各层拿配置一律通过 ``builtin/config/typed.py`` 的 typed dict 加载器
（``load_typed``），不再直接读 ``config.static.*``。

本模块只保留声明 + 旧版描述符 API 的兼容入口；
主题补全的实现在 ``builtin/config/typed.py``。
"""

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from albuswall.configue import (
    ConfigField,
    PathField,
    register_dynamic_state,
)

if TYPE_CHECKING:
    from albuswall.configue import Configue


class WindowPresenterConfs:
    # 让 files.qss 有真实默认值，ui.style_sheet 通过插值解析到它
    qss_file_name: str = ConfigField("files", "qss", default="main_window.qss")
    qss: Optional[Path] = PathField(
        "ui", "style_sheet", default="${path:theme}/${files:qss}")
    icon_dir: Optional[Path] = PathField("path", "icon", default=None)
    preference_config_node: str = ConfigField(
        "ui", "preference_config_node", default="ui.preference")
    preference_file_name: str = ConfigField("files", "preference", default="preference")
    preference_file: Optional[Path] = PathField("ui", "preference_file", default=None)
    window_state_file_name: str = ConfigField("files", "window_state", default="window.json")

    auto_complete: bool = ConfigField("ui", "auto_complete_theme", default=True)
    auto_complete_qss: bool = ConfigField("ui", "auto_complete_qss", default=True)
    auto_complete_icons: bool = ConfigField("ui", "auto_complete_icons", default=True)
    auto_complete_only_once: bool = ConfigField("ui", "auto_complete_only_once", default=True)

    # API（兼容入口）
    def ensure_theme_files_completed(
            self, config: Optional["Configue"] = None,
    ) -> None:
        """兼容入口：按 typed dict 把内置主题缺失的文件补到用户主题目录。

        取值改走 ``window_config()``，``config`` 参数只为兼容旧调用保留。
        """
        from .typed import ensure_theme_files_completed as _ensure

        _ensure()


class AlbumPresenterConfs:
    window_title: Optional[str] = ConfigField("ui", "window_title", default=None)


class ThumbnailGridPresenterConfs:
    favorite_on_icon: Optional[Path] = PathField(
        "ui", "favorite_on_icon", default="${path:icon}/favorite_icon.svg")


# ══════════════════════════════════════════════════
# 动态子树登记（窗口几何）
# ══════════════════════════════════════════════════
# 本模块在插件 setup() 阶段就被 import（早于静态配置加载），而状态类住在
# ``presenter.window_presenter``——它要等配置就绪才 import。所以这里手动指定：
#
#   - cls 用 "pkg.mod:Class" 字符串 → 首次访问 config.dynamic.window 时才 import
#   - filename 用 ConfigField 声明 → 访问期从静态配置取 files.window_state
#
# autosave=False：几何变更由 WindowPresenter 做 debounce 后统一 save()。
register_dynamic_state(
    "window",
    "albuswall.ui.builtin.presenter.window_presenter:WindowState",
    ("path", "cache"),
    WindowPresenterConfs.window_state_file_name,
    autosave=False,
)
