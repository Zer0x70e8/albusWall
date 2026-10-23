#
"""UI 配置层。

对 UI 而言，配置的唯一使用形态是 **typed dict**：

.. code-block:: python

    from albuswall.ui.builtin.config import album_config

    conf = album_config()
    conf["window_title"]

typed dict 的形状在 ``albuswall.ui.schema``，默认值 / 类型来自
``albuswall.configue`` 的静态配置声明，映射逻辑在
``builtin/config/typed.py``。

``WindowPresenterConfs`` / ``PreferenceConfig`` 等描述符类保留为兼容层
（同时充当声明来源）。

设置页需要的「分组 → 叶子 → 配置节点」结构登记属于配置层通用能力，见
``albuswall.configue.setting_schema``；UI 只是它的一个消费方。
"""

from albuswall.ui.schema import (
    AlbumConfig,
    PreferenceSourceConfig,
    ThumbnailGridConfig,
    UIBootstrapConfig,
    WindowConfig,
)

from .icons import resolve_icon_dir, resolve_icon_file
from .preference import PreferenceConfig
from .static import (
    AlbumPresenterConfs,
    ThumbnailGridPresenterConfs,
    WindowPresenterConfs,
)
from .typed import (
    album_config,
    ensure_theme_files_completed,
    preference_source_config,
    thumbnail_grid_config,
    window_config,
)

__all__ = [
    # typed dict（推荐用法）
    "AlbumConfig",
    "PreferenceSourceConfig",
    "ThumbnailGridConfig",
    "UIBootstrapConfig",
    "WindowConfig",
    "album_config",
    "ensure_theme_files_completed",
    "preference_source_config",
    "resolve_icon_dir",
    "resolve_icon_file",
    "thumbnail_grid_config",
    "window_config",
    # 兼容层（同时是声明来源）
    "AlbumPresenterConfs",
    "PreferenceConfig",
    "ThumbnailGridPresenterConfs",
    "WindowPresenterConfs",
]
