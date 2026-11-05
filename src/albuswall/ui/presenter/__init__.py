#
"""``albuswall.ui.presenter`` —— UI 表现层（视图 ↔ 服务之间那一层）。

子包结构
========

``base``                  :class:`Presenter` 基类：父子树 + 幂等生命周期 +
                          子 presenter 登记表 + 依赖读取
``main``                  :class:`MainPresenter`：根节点，编排子 presenter、
                          跨 presenter 接线、窗口主题
``theme``                 窗口主题解析：``[ui]`` 的 ``${...}`` 组合（样式表 +
                          图标路径，见 :mod:`albuswall.ui.presenter.theme`）

窗口各区域的 presenter（登记顺序即启动顺序）::

    window_presenter.py        window      浮层焦点调度 / 窗口级 toast
    album_presenter.py         album       相册列表 / 当前相册 / 标题栏
    content_presenter.py       content     主内容网格（按需缩略图 + 标签过滤）
    viewer_presenter.py        viewer      详情页（大图 / 导航 / 收藏 / 软删恢复）
    tagbar_presenter.py        tagbar      当前资产的标签 chip + 标签树
    tag_editor_presenter.py    tag_editor  标签新建 / 重命名 / 删除
    source_presenter.py        source      导入源页（卡片 + 表单）
    search_presenter.py        search      浮动搜索栏（按标签过滤）
    source_form.py            ——          表单 ⇄ DTO 的桥（非 presenter）
    shortcuts.py              ——          界面动作 → 快捷键槽（keymap）登记

视图层（``albuswall.ui.window`` / ``widgets``）只画界面、只发意图信号；
presenter 才是读配置、调服务、把结果写回视图的地方。新增 presenter 的姿势：

.. code-block:: python

    from albuswall.ui.presenter import Presenter, register_presenter

    @register_presenter
    def _build_album_presenter(container, main):
        return MyPresenter(container, window=main.window, parent=main)

主 presenter 启动时按登记顺序构造并启动它，退出时逆序停止；presenter 之间
**不互相持有**，跨 presenter 的流程由主 presenter 在 ``_wire()`` 里用 Qt
信号连接（见 :mod:`albuswall.ui.presenter.main`）。

应用类入口只有一个：``MainPresenter.bootstrap(container, app=..., window=...)``
（见 :meth:`albuswall.ui.presenter.main.MainPresenter.bootstrap`）。
"""

from __future__ import annotations

from .album_presenter import AlbumPresenter
from .base import (
    PRESENTER_FACTORIES,
    Presenter,
    PresenterFactory,
    register_presenter,
    registered_presenters,
)
from .content_presenter import ContentPresenter
from .main import MainPresenter
from .search_presenter import SearchPresenter
from .shortcuts import UI_SLOTS, install_ui_shortcuts
from .source_presenter import SourcePresenter
from .tag_editor_presenter import TagEditorPresenter
from .tagbar_presenter import TagBarPresenter
from .theme import (
    DEFAULT_THEME,
    ICON_KEYS,
    THEME_QSS_NAME,
    Theme,
    ThemeConfig,
    ThemeLayout,
    available_themes,
    configured_theme_qss,
    resolve_theme,
)
from .viewer_presenter import ViewerContext, ViewerPresenter
from .window_presenter import WindowPresenter

__all__ = [
    "DEFAULT_THEME",
    "ICON_KEYS",
    "PRESENTER_FACTORIES",
    "THEME_QSS_NAME",
    "AlbumPresenter",
    "ContentPresenter",
    "MainPresenter",
    "Presenter",
    "PresenterFactory",
    "SearchPresenter",
    "UI_SLOTS",
    "SourcePresenter",
    "TagBarPresenter",
    "TagEditorPresenter",
    "Theme",
    "ThemeConfig",
    "ThemeLayout",
    "ViewerContext",
    "ViewerPresenter",
    "WindowPresenter",
    "available_themes",
    "configured_theme_qss",
    "install_ui_shortcuts",
    "register_presenter",
    "registered_presenters",
    "resolve_theme",
]
