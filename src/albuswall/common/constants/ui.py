#
"""界面主题的**目录骨架**常量（主题名 / 入口样式表文件名 / 图标子目录名）。

为什么单独一个模块
==================

这三个名字同时被两处以**不同时机**使用：

* :mod:`albuswall.configue.bootstrap` 在 **``load()`` 之前**就要声明
  ``[ui] theme`` 的默认值——``[path]`` / ``[files]`` / ``[ui]`` 里的
  ``${ui:theme}`` 插值在 ``check_all()`` 阶段就会展开，晚于加载才声明会让
  用户写在文件里的组合式失效（``[ui] theme`` 找不到 → ``KeyError``）；
* :mod:`albuswall.ui.presenter.theme` 在 UI import 之后才读这些值。

``configue`` 不能 import ``ui``（会拖进 PySide6，且那时 UI 还没装配），所以
「同一份常量」只能落在两层的公共上游：``common``。依赖方向仍是
``common ← (configue / ui)``，本模块**不得**反向 import 其它层。

布局约定（随包主题与用户主题目录**同构**）
==========================================

::

    <theme root>/<name>/main_window.qss     # 入口样式表
    <theme root>/<name>/icons/<icon files>  # 主题图标

* 随包主题：``resources/themes/<name>/…``（``albuswall.resources.theme``）；
* 用户主题：``[path] theme`` = ``${path:config}/theme``，即
  ``~/.config/albuswall/theme/<name>/…``，``[path] icons`` = ``${theme}/icons``
  是它的图标根目录（``<icons>/<name>/<file>``）。

``[files]`` 里的文件名（``main_window_qss`` / 各图标文件名）与 ``[ui]`` 里的
组合式（``${path:theme}/${ui:theme}/${files:main_window_qss}`` 等）在随包
``resources/config/conf.ini`` 与 :class:`albuswall.ui.presenter.theme.ThemeConfig`
的默认值里**逐字一致**；本模块只提供兜底名字（配置不可用 / 未声明时）。
"""

__all__ = [
    "DEFAULT_THEME",
    "ICON_DIR_NAME",
    "ICON_KEYS",
    "ICON_SUFFIX",
    "THEME_QSS_NAME",
]

#: 缺值 / 未知主题名时的回退主题。
DEFAULT_THEME = "default"

#: 每个主题目录里的入口样式表文件名（``[files] main_window_qss`` 的兜底）。
THEME_QSS_NAME = "main_window.qss"

#: 主题目录里的图标子目录名（``[path] icons`` 组合式的最后一段）。
ICON_DIR_NAME = "icons"

#: 图标名不带扩展名时尝试的默认扩展名（``icon("album_button")``）。
ICON_SUFFIX = ".svg"

#: 可配置的图标逻辑名：``[files] <key>`` 是文件名，``[ui] <key>`` 是与
#: ``${path:icons}`` / ``${ui:theme}`` 组合出来的绝对路径，UI 侧按
#: ``Theme.icons[<key>]`` 取用（键名 === 图标逻辑名 === 配置键名）。
ICON_KEYS: tuple[str, ...] = (
    "app_icon",
    "album_button",
    "search_button",
    "more_button",
    "favorite_icon",
)
