#
"""界面 boot 层：把被动的 Qt 主循环装成一把能用的界面。

对位关系
========

``main.py::_boot`` 编排**宿主**启动的 phase 序列（config → plugins → logger →
main loop → gc → 基础设施 → 服务）；本模块编排**界面**那一半。两边都是
「先决条件就绪 → 装配 → 登记进容器」的同一形状，调用方是
``albuswall.plugins.builtin_ui``：

=========================  ===================================================
:func:`setup_ui`           **init** 阶段：phase 1 语言 + ``ui_app`` / ``i18n``
                           → phase 2 主窗口 → phase 3 快捷键注册表（容器登记）
                           → phase 4 presenter（``MainPresenter.bootstrap``）
                           → phase 5 界面动作登记成快捷键槽 + 载入键位表
                           + 装上按键过滤器（binder）
:func:`show_ui`            **loop** 阶段：把容器里的主窗口显示出来
=========================  ===================================================

调用时机不是自由选择：``init`` 时 ``main._boot`` 的 phase 6/7 已经把基础设施
与服务装好（presenter 树要它们才起得来）；``loop`` 时插件已经把界面内容加完
（早显示只会闪）。

容器键（界面侧唯一的公共契约）
==============================

===============  ==========================================================
键               值
===============  ==========================================================
``ui_app``       被动 ``QApplication``（:class:`.application.Application`）
``i18n``         界面语言管理器（与模块级 :func:`albuswall.ui.set_language` 同一对象）
``main_window``  主窗口（默认 :class:`~albuswall.ui.window.Window`）
``presenter``    presenter 树的根（``main_presenter`` 是别名）
``shortcuts``    快捷键注册表（进程级单例；phase 5 登记完界面槽之后会
                 ``load_shortcuts()`` 一次）
``shortcuts_binder`` 按键过滤器（``QApplication`` 上唯一的那一个；phase 5
                 装上，作用域 = 主窗口。导航键 / Esc / 文本输入 / 控件
                 ``keymap_passthrough`` 都会被它主动让渡）
===============  ==========================================================

换主窗口 = 换容器里的 ``main_window``
====================================

:func:`setup_ui` **不**自己 new 窗口，而是先问容器
:func:`ensure_main_window`：容器里已经有主窗口就直接用，没有才建默认的
:class:`~albuswall.ui.window.Window`。所以派生窗口不需要改 ``ui/window/``
一行代码，两种写法：

.. code-block:: python

    # ① 工厂：setup 阶段就能写（QWidget 还没法构造，懒到 UI boot 再实例化）
    container.register("main_window", MyWindow)       # 类 → 零参构造
    container.register("main_window", lambda c: MyWindow(c))

    # ② 实例：必须等 QApplication 存在之后——即 ``boot`` hook 里
    #    （main._boot 的 phase 4 已 resolve 主循环；``setup`` 阶段还没有 Qt 应用）
    @ctx.on("boot")
    def on_boot() -> None:
        container.set("main_window", MyWindow())

窗口的显示同样只认容器：:func:`show_ui` 在 ``loop`` 阶段现取
``main_window`` 再 ``show()``，因此「延后显示」不会显示到过期的那个对象上。

顺序上的两条硬约束
==================

1. **先定语言再建窗口**：控件一出生就是目标语言的文案，不用等一次
   ``LanguageChange`` 重绘；
2. **presenter 先于 show**：主题在窗口显示之前套上，避免窗口先以无主题的
   样子闪一下。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from albuswall import __title__
from albuswall.configue import configclass

from .i18n import TranslationManager
from .keymap import install_shortcut_binder, registry_shortcut_registry
from .languages import DEFAULT_LANGUAGE
from .presenter import MainPresenter
from .presenter.shortcuts import install_ui_shortcuts
from .utils import install_activation_key_filter
from .window import Window

if TYPE_CHECKING:
    from albuswall.core.container import Container

    from .application import Application

__all__ = [
    "I18N_KEY",
    "MAIN_WINDOW_KEY",
    "SHORTCUT_BINDER_KEY",
    "UI_APP_KEY",
    "UIConf",
    "configured_language",
    "ensure_main_window",
    "setup_ui",
    "show_ui",
]

logger = logging.getLogger(__package__)

#: 默认窗口标题与尺寸。
DEFAULT_TITLE = __title__
DEFAULT_SIZE = (960, 720)

#: 容器键：被动 Qt 应用（``QApplication`` + ``MainLoop``）。
UI_APP_KEY = "ui_app"
#: 容器键：界面语言管理器。
I18N_KEY = "i18n"
#: 容器键：主窗口（默认 :class:`~albuswall.ui.window.Window`；可被派生类替换）。
MAIN_WINDOW_KEY = "main_window"
#: 容器键：按键过滤器（``QApplication`` 上唯一的那一个）。
SHORTCUT_BINDER_KEY = "shortcuts_binder"


# noinspection bad-assignment
@configclass("ui", "main")
class UIConf:
    """``[ui]`` 段：界面偏好（当前只有启动语言）。

    ``albuswall.ui`` 是在插件发现阶段（``setup_config`` + ``load()`` 之后）
    才被 import 的，装饰晚于 ``load()`` 的 Step 4；``configclass`` 对
    「``Configue`` 已构造」的类会在装饰当下立即注入类型与默认值
    （``inject_raw(overwrite=False)``，文件里写了的值照样赢），所以这里可以
    直接写默认值。

    声明本身的价值：``(section, key)`` 进了声明表，``load(strict_undeclared
    =True)`` 不会把它当幽灵 key，拼错的 ``[ui] languge`` 也读不到值。
    """

    language: str = DEFAULT_LANGUAGE


def configured_language() -> str:
    """读 ``[ui] language`` 作为启动语言；空白 / 缺值时用默认语言。

    用 :class:`UIConf` 类视图而不是 ``config.tree``：key 在声明期就注册，
    默认值由 ``configclass`` 注入 Resolver。``getattr`` 只是防御性兜底
    （``ConfigMissingError`` 继承 ``AttributeError``）：单测里 UI 可能被
    单独加载，此时没有 ``Configue`` 单例。
    """
    value = getattr(UIConf(), "language", DEFAULT_LANGUAGE)
    text = str(value or "").strip()
    return text or DEFAULT_LANGUAGE


def ensure_main_window(container: Container) -> Window:
    """取主窗口：容器里已有就用它，没有才建默认的 :class:`Window`。

    「已有」包含两种写法，都走容器自己的解析规则（``register`` 的工厂会被
    ``get`` 懒调用），所以派生窗口不必改 ``ui/window/``：

    .. code-block:: python

        container.set("main_window", MyWindow())          # 现成实例
        container.register("main_window", MyWindow)        # 类 → 零参构造

    来自容器的窗口**自己说了算**：本函数只补它没设过的默认值（空标题 →
    :data:`DEFAULT_TITLE`，零尺寸 → :data:`DEFAULT_SIZE`），不覆盖它已经调过
    的 ``setWindowTitle`` / ``resize``。
    """
    window = container.get(MAIN_WINDOW_KEY)
    if window is None:
        window = Window()
        container.set(MAIN_WINDOW_KEY, window)
        # 默认窗口：标题与尺寸是 boot 的职责（派生窗口自己管）
        window.setWindowTitle(DEFAULT_TITLE)
        window.resize(*DEFAULT_SIZE)
        logger.debug("ui.bootstrap: default main window created")
        return window

    if not window.windowTitle():
        window.setWindowTitle(DEFAULT_TITLE)
    if window.size().isEmpty():
        window.resize(*DEFAULT_SIZE)
    logger.debug(
        "ui.bootstrap: main window taken from container (%s)",
        type(window).__name__)
    return window


def setup_ui(container: Container, app: Application) -> Window:
    """界面 boot：语言 → 主窗口 → 快捷键注册表 → presenter → 快捷键槽。

    由 ``albuswall.plugins.builtin_ui`` 在 ``init`` hook 里调用（hook 插在
    ``init`` 列表头部）。这个时机是**必须**的：

    * 晚于 ``main._boot`` 的 phase 6/7 —— 基础设施与服务都已进容器，
      presenter 树才拿得到自己依赖的服务；
    * 早于其它插件的 ``on_init`` —— 它们可以直接
      ``container.get("main_window")`` / ``container.get("presenter")``。

    窗口来自 :func:`ensure_main_window`：容器里已经塞了派生窗口就用那个，
    这里不 new、不认类型。

    Args:
        container: 依赖容器（与 ``app`` 同属一次启动）。
        app: 已创建好的被动 Qt 应用；本函数只经 ``MainPresenter`` 往它身上挂
            ``aboutToQuit`` 之类的连接，不改变它的被动性质。

    Returns:
        主窗口（同时登记为 ``container["main_window"]``）。
    """
    # phase 1: 语言（先定语言再建窗口）
    container.set(UI_APP_KEY, app)
    # 激活键：空格 / 回车在按钮与列表上等价（应用级过滤器，幂等）。
    # 详见 albuswall.ui.utils.activation_keys。
    install_activation_key_filter(app)
    translations = TranslationManager(app)
    container.set(I18N_KEY, translations)
    translations.set_language(configured_language())

    # phase 2: 主窗口（容器优先：派生窗口替换的落点）
    window = ensure_main_window(container)

    # phase 3: 界面侧注册表（快捷键）。**延后**到界面 boot 才登记：它只对界面
    # 有意义，不该占宿主 ``main._boot`` 的 phase 6/7（那是基础设施与服务）。
    registry_shortcut_registry(container)

    # phase 4: presenter 树（在 show 之前套主题，绑定主窗口）
    MainPresenter.bootstrap(container, app=app, window=window)

    # phase 5: 界面动作 → 快捷键槽 → 载入键位表 → 装按键过滤器。
    # 顺序有讲究：先有槽（handler）再载入（键位），最后才让 binder 接管按键；
    # binder 装在 activation_key_filter（phase 1）之后，Qt 后装先跑，所以
    # 真实按键先过 binder，被改写的合成空格（非自发事件）不会再触发一遍。
    try:
        # 键位表的真身是配置器的 ``[shortcuts]`` 段（``setup_config`` 把随包
        # 默认表与用户覆盖表登记成两个文件）。把注册表订阅进去，之后改值
        # （设置页 / 命令 / 外部重载）都会立即重建绑定。
        shortcuts = install_ui_shortcuts(container, config=container.get("config"))
        binder = install_shortcut_binder(app, shortcuts, owner=window)
        container.set(SHORTCUT_BINDER_KEY, binder)
    except Exception:  # noqa: BLE001 - 键位配置坏了不该拦住界面启动
        logger.exception("ui.bootstrap: installing UI shortcuts failed")

    logger.debug("ui.bootstrap: main window built")
    return window


def show_ui(container: Container) -> None:
    """显示主窗口；没有窗口时静默跳过（headless / 没跑过 :func:`setup_ui`）。

    窗口在调用**当下**从容器取：``loop`` 阶段之前换掉 ``main_window`` 也能显示
    到对的那个对象上（替换窗口的时机要求见模块 docstring）。
    """
    window = container.get(MAIN_WINDOW_KEY)
    if window is None:
        logger.debug("ui.bootstrap: no main window to show")
        return
    window.show()
    logger.debug("ui.bootstrap: main window shown (%s)", type(window).__name__)
