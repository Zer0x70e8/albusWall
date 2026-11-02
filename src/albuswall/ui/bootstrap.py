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
                           → phase 2 主窗口 → phase 3 导航注册表（3a 容器登记
                           + 3b 窗口结构 → 域 / 节点）→ phase 4 presenter
                           （``MainPresenter.bootstrap``）
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
``nav_registry`` 导航注册表（进程级单例；phase 3a 登记进容器，phase 3b
                 由 :func:`~albuswall.ui.nav.wiring.register_window_nav` 写入
                 主窗口的域 / 节点）；由 ``NavPresenter``（presenter 树里的
                 ``nav``）负责按键分派
``shortcuts``    快捷键注册表（进程级单例；phase 3a 只**登记**，**不**自动
                 ``load_shortcuts()``——加载时机仍归调用方）
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
from .languages import DEFAULT_LANGUAGE
from .keymap import registry_shortcut_registry
from .nav import NAV_PAGE_ID, register_window_nav, registry_nav_registry
from .presenter import MainPresenter
from .window import Window

if TYPE_CHECKING:
    from albuswall.core.container import Container

    from .application import Application

__all__ = [
    "I18N_KEY",
    "MAIN_WINDOW_KEY",
    "NAV_REGISTRY_KEY",
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
#: 容器键：导航注册表（``albuswall.ui.nav`` 的进程级单例）。
NAV_REGISTRY_KEY = "nav_registry"


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


def _land_initial_focus(nav: object, window: object) -> None:
    """把光标落到根域第一个可见入口，并执行一次焦点（``setup_ui`` phase 3c）。

    「首焦点由谁决定」在 nav 的契约里是明确分工的：``register_window_nav``
    只负责复位（``jump_to_page_root`` → 根域虚拟根 ``current=None``），**不**
    替调用方选落点；选落点属于 boot 的编排。这里的落点由 nav 自己算
    （``enter(how="tab")`` 的三级 fallback），boot 不硬编码任何节点 id——
    派生窗口砍掉标题栏时，落点会自动变成下一个可见入口。

    ``enter`` 返回 ``None``（根域一个可见落点都没有）时安静跳过：headless /
    游戏化窗口里没有可聚焦控件是允许的状态。

    Args:
        nav: 导航注册表（``NavRegistry``；这里只按鸭子类型调用三个方法）。
        window: 主窗口，仅用于日志。
    """
    try:
        node = nav.enter(NAV_PAGE_ID, how="tab")
    except Exception:  # noqa: BLE001 - boot 不该因为导航落点把启动拖崩
        logger.debug("ui.bootstrap: initial nav landing failed", exc_info=True)
        return
    if node is None:
        logger.debug(
            "ui.bootstrap: no focusable entry for %s; leaving focus alone",
            type(window).__name__)
        return
    try:
        nav.focus_node(node)
    except Exception:  # noqa: BLE001 - 窗口还没显示时 setFocus 可能不可用
        logger.debug(
            "ui.bootstrap: initial focus on %s failed", node.id, exc_info=True)


def setup_ui(container: Container, app: Application) -> Window:
    """界面 boot：语言 → 主窗口 → 导航注册表 → presenter。

    由 ``albuswall.plugins.builtin_ui`` 在 ``init`` hook 里调用（hook 插在
    ``init`` 列表头部）。这个时机是**必须**的：

    * 晚于 ``main._boot`` 的 phase 6/7 —— 基础设施与服务都已进容器，
      presenter 树才拿得到自己依赖的服务；
    * 早于其它插件的 ``on_init`` —— 它们可以直接
      ``container.get("main_window")`` / ``container.get("presenter")``。

    窗口来自 :func:`ensure_main_window`：容器里已经塞了派生窗口就用那个，
    这里不 new、不认类型。

    phase 3b 之后还要做一次「首焦点」（见 :func:`_land_initial_focus`）：
    wiring 结束时光标停在根域虚拟根，方向键在那上面没有落点，不落首焦点
    会表现成「界面出来了，方向键按了没反应」。

    Args:
        container: 依赖容器（与 ``app`` 同属一次启动）。
        app: 已创建好的被动 Qt 应用；本函数只经 ``MainPresenter`` 往它身上挂
            ``aboutToQuit`` 之类的连接，不改变它的被动性质。

    Returns:
        主窗口（同时登记为 ``container["main_window"]``）。
    """
    # phase 1: 语言（先定语言再建窗口）
    container.set(UI_APP_KEY, app)
    translations = TranslationManager(app)
    container.set(I18N_KEY, translations)
    translations.set_language(configured_language())

    # phase 2: 主窗口（容器优先：派生窗口替换的落点）
    window = ensure_main_window(container)

    # phase 3: 界面侧注册表（导航 + 快捷键）。**延后**到界面 boot 才登记：它们
    # 只对界面有意义，不该占宿主 ``main._boot`` 的 phase 6/7（那是基础设施与服务）。
    #
    # 3a 容器登记 → 3b 把窗口结构注册成域 / 节点。顺序不能反：wiring 要往
    # 容器里那**一个**注册表写（进程级单例），先登记才能取到同一个对象。
    registry_nav_registry(container)
    registry_shortcut_registry(container)
    nav = container.get(NAV_REGISTRY_KEY)
    register_window_nav(window, registry=nav)
    # 3c 首焦点。``register_window_nav`` 只把页面复位到根域虚拟根（``current``
    # 为 ``None``）；虚拟根上按方向键没有任何落点，光标会「按了没反应」。所以
    # 「第一次按键往哪走」由 boot 决定：落根域第一个可见 focusable（正常情况下
    # 是 ``window.titlebar`` 区域入口），并把真实焦点交给它的 ``focus_fn``。
    _land_initial_focus(nav, window)

    # phase 4: presenter 树（在 show 之前套主题，绑定主窗口）
    MainPresenter.bootstrap(container, app=app, window=window)

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
