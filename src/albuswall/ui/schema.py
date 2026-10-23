#
"""UI 层配置的 typed dict 形状。

**只声明「形状 + 类型」，不写默认值**：默认值、类型转换一律由
``albuswall.configue`` 的静态配置声明（``ConfigField`` / ``PathField``）提供。
键名与声明类的属性名一一对应，取值统一走
``albuswall.configue.typed.load_typed``（见 ``builtin/config/typed.py``）。

对应关系：

===========================  ==========================================
typed dict                   声明来源（静态配置部分）
===========================  ==========================================
``WindowConfig``              ``WindowPresenterConfs``
``AlbumConfig``               ``PreferenceConfig``（偏好层优先）+ 静态字段
``ThumbnailGridConfig``       ``ThumbnailGridPresenterConfs``
``UIBootstrapConfig``         ``UIConfig`` + ``debug``
``PreferenceSourceConfig``    ``WindowPresenterConfs`` + ``path.data`` 内建字段
===========================  ==========================================

注意 ``WindowConfig["auto_complete"]`` 对应 INI 里的
``[ui] auto_complete_theme``：键名跟声明类的属性名走，节 / 键名由声明里的
path 决定。
"""

from pathlib import Path
from typing import Any, Optional, TypedDict, Union

__all__ = [
    "AlbumConfig",
    "PreferenceSourceConfig",
    "ThumbnailGridConfig",
    "UIBootstrapConfig",
    "WindowConfig",
]

# 偏好里的路径字段既接受绝对路径也接受相对图标目录的文件名
PathLike = Union[Path, str]


# ══════════════════════════════════════════════════
# 主窗口 / 主题（WindowPresenterConfs）
# ══════════════════════════════════════════════════
class WindowConfig(TypedDict):
    """窗口外观 + 主题自动补全相关的静态配置。"""

    qss: Optional[Path]
    qss_file_name: str
    icon_dir: Optional[Path]
    preference_config_node: str
    preference_file: Optional[Path]
    preference_file_name: str
    window_state_file_name: str
    auto_complete: bool
    auto_complete_qss: bool
    auto_complete_icons: bool
    auto_complete_only_once: bool


# ══════════════════════════════════════════════════
# 标题栏 / 专辑 / 图标（PreferenceConfig）
# ══════════════════════════════════════════════════
class AlbumConfig(TypedDict):
    """标题栏与专辑面板用到的偏好字段（图标名按图标目录解析）。"""

    theme: str
    theme_dir: Optional[PathLike]
    style_sheet: Optional[PathLike]
    icon_dir_pref: Optional[PathLike]
    window_title: Optional[str]
    album_icon: Optional[PathLike]
    extra_icon: Optional[PathLike]
    search_icon: Optional[PathLike]
    default_album_uuid: Optional[str]
    search_expanded: bool
    album_text: str
    extra_text: str
    search_placeholder: str


# ══════════════════════════════════════════════════
# 缩略图网格（ThumbnailGridPresenterConfs）
# ══════════════════════════════════════════════════
class ThumbnailGridConfig(TypedDict):
    """缩略图网格的装饰用图标。"""

    favorite_on_icon: Optional[Path]


# ══════════════════════════════════════════════════
# UI 引导（UIConfig，见 albuswall.ui.bootstrap）
# ══════════════════════════════════════════════════
class UIBootstrapConfig(TypedDict):
    """选哪个 UI 实现 / 用哪套主题 / 是否开 debug。

    ``debug`` 与 ``albuswall.__main__.Conf`` 指向同一个节点
    （``__default__`` 节的 ``debug``），这里只补类型，默认值仍由静态声明给出。
    """

    theme_path: Optional[str]
    main: dict[str, Any]
    theme: str
    active: str
    language: str
    no_builtin: bool
    debug: bool


# ══════════════════════════════════════════════════
# 偏好文件定位（UI 配置 setup 用）
# ══════════════════════════════════════════════════
class PreferenceSourceConfig(TypedDict):
    """preference.ini 的落点：``ui.preference_file`` 优先，否则 data 目录。

    ``data`` 是 ``path.data`` 内建字段（``setup_static_config`` 注册）的
    属性名。
    """

    preference_file: Optional[Path]
    preference_file_name: str
    data: Path
