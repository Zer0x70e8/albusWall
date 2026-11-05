#
"""把当前界面的动作登记成快捷键槽（UI → ``albuswall.ui.keymap``）。

本模块是「界面」与「快捷键配置器」之间**唯一**的接线点，只做两件事：

1. **登记接口**：把 :data:`UI_SLOTS` 里的命令逐个登记成 ``ShortcutSlot``，
   处理函数是 presenter 树上的**公开方法**（``album.create_album`` /
   ``viewer.zoom_in`` / ……），不是视图信号，也不是服务调用；
2. **载入键位**：登记完调一次 ``load_shortcuts()``，让
   ``resources/config/shortcuts.ini`` + 用户覆盖表接管键位。

之后 :meth:`ShortcutRegistry.trigger` / :meth:`ShortcutRegistry.invoke` 就能
驱动这套界面：

.. code-block:: python

    from albuswall.ui.keymap import get_default_registry

    get_default_registry().trigger("Ctrl+N")    # == album.create_album()
    get_default_registry().invoke("album.new")  # 同上，按命令触发

**不接管真实按键**：``QKeyEvent`` → PortableText → ``trigger()`` 的转换归
:class:`albuswall.ui.keymap.qt_binder.ShortcutBinder`（界面 boot 的 phase 5
一起装），本模块只登记「命令 → presenter 方法」这一层。

``asset.favorite`` 的默认键是 ``Ctrl+D`` 而不是 ``Space``：空格在内容网格里
是「快速预览」、在按钮上是「点击」，属于控件自己的键（binder 会主动让渡）。

默认**不绑键位**的 6 条命令（``window.album`` / ``window.source`` /
``viewer.next`` / ``viewer.prev`` / ``content.preview`` / ``asset.restore``）
在随包表里写成 ``= []``：功能是真的，只是没给键。想用就在
``<user config dir>/shortcuts.ini`` 里把 ``[]`` 换成自己的键位组合。

懒解析，不持有 presenter
========================

槽的处理函数在**触发时**才从容器取 ``presenter``（``container["presenter"]``
是 :class:`~albuswall.ui.presenter.main.MainPresenter`，子节点按
:attr:`~albuswall.ui.presenter.base.Presenter.name` 定位），因此：

* 登记可以在 presenter 构建之前／之后做，顺序无关；
* 界面换一套 presenter 树（测试、插件热替换）不用重新登记槽；
* 没有界面（headless / 单测里只有容器）时，触发是**静默 no-op**，处理函数
  返回 ``False``，不抛 ``KeyError``——键位层不该因为「界面还没起」而炸。

命令与落点
==========

:data:`UI_SLOTS` 与 ``resources/config/shortcuts.ini`` 的 18 条命令一一对应；
``default_keys`` 只是**兜底键位**，配置文件里出现同名命令时以配置为准。

命令               落点（presenter.方法）
-----------------  ----------------------------------------------
``app.quit``       ``window.quit``
``app.settings``   ``window.open_settings``
``window.fullscreen`` ``window.toggle_fullscreen``
``window.close``   ``window.close_layer``
``window.album``   ``window.toggle_album``
``window.source``  ``window.toggle_source``
``view.zoom_in``   ``viewer.zoom_in``
``view.zoom_out``  ``viewer.zoom_out``
``view.reset``     ``viewer.reset_zoom``
``viewer.next``    ``viewer.next_asset``
``viewer.prev``    ``viewer.previous_asset``
``content.search`` ``search.focus_search``
``content.preview`` ``content.preview_current``
``album.new``      ``album.create_album``
``album.rename``   ``album.rename_current``
``asset.favorite`` ``viewer.toggle_favorite``（详情页打开时）/ ``content.toggle_favorite``
``asset.restore``  ``viewer.restore_current``（详情页打开时）/ ``content.restore_current``
``asset.delete``   ``viewer.trash_current``（详情页打开时）/ ``content.trash_current``

``asset.*`` 是「对当前资产」的动作：详情页开着就作用于详情页里的那张图，
否则作用于内容网格光标所在的那一格。

返回值约定
==========

每个处理函数返回 ``bool``：``True`` = 这次触发真的改变了一点东西
（开了全屏、写了库、抢到焦点……），``False`` = 没有界面 / 没有当前资产 /
环境不满足（例如详情页没开时的缩放）。``trigger()`` 把各命令的返回值组成
元组返回，调用方可以据此判断「这一下按了有没有用」。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from albuswall.ui.keymap import (
    ShortcutRegistry,
    ShortcutSlot,
    get_default_registry,
    load_shortcuts,
)

if TYPE_CHECKING:
    from albuswall.core.container import Container

__all__ = [
    "UI_SLOTS",
    "install_ui_shortcuts",
    "shortcut_registry_of",
]

logger = logging.getLogger(__package__)

#: 容器键：presenter 树的根（``MainPresenter.bootstrap`` 写入，别名
#: ``main_presenter``）。
PRESENTER_KEY = "presenter"
#: 容器键：快捷键注册表（``registry_shortcut_registry`` 写入）。
SHORTCUTS_KEY = "shortcuts"

#: :data:`UI_SLOTS` 里 ``target`` 的哨兵值：按「当前资产」决定落点。
CURRENT_ASSET = "*"


# ======================================================================
# 懒解析：容器 → presenter
# ======================================================================

def _main_presenter(container: Container) -> Any | None:
    """容器的 ``presenter``；没有界面时返回 ``None``。"""
    try:
        return container.get(PRESENTER_KEY)
    except Exception:  # noqa: BLE001 - 取不到依赖 = 没有界面
        logger.exception("shortcuts: cannot resolve the presenter tree")
        return None


def _child(container: Container, name: str) -> Any | None:
    """按名字取子 presenter（``Presenter.child``）；取不到返回 ``None``。"""
    main = _main_presenter(container)
    if main is None:
        return None
    getter = getattr(main, "child", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:  # noqa: BLE001 - 单个 presenter 坏了不扩散
        logger.exception("shortcuts: cannot resolve presenter %r", name)
        return None


def _asset_presenter(container: Container) -> Any | None:
    """``asset.*`` 的落点：详情页开着用 viewer，否则用内容网格。

    「开着」以 :attr:`~albuswall.ui.presenter.viewer_presenter.ViewerPresenter
    .is_detail_open` 为准（浮层当前页确实是详情页），而不是只看它有没有
    上一次的 ``context``——浮层被收起后 ``context`` 还在。
    """
    viewer = _child(container, "viewer")
    if viewer is not None and bool(
            getattr(viewer, "is_detail_open", False)):
        return viewer
    return _child(container, "content")


def _call_on(presenter: Any | None, method: str) -> bool:
    """调 presenter 的公开方法；没有 presenter / 没有方法时静默返回 ``False``。"""
    if presenter is None:
        logger.debug("shortcuts: no presenter for %s(); skipped", method)
        return False
    handler = getattr(presenter, method, None)
    if not callable(handler):
        logger.debug(
            "shortcuts: %s has no %s(); skipped",
            type(presenter).__name__, method)
        return False
    try:
        return bool(handler())
    except Exception:  # noqa: BLE001 - 快捷键不该把异常抛回事件循环
        logger.exception(
            "shortcuts: %s.%s() failed", type(presenter).__name__, method)
        return False


# ======================================================================
# 槽登记表
# ======================================================================

@dataclass(frozen=True, slots=True)
class _SlotSpec:
    """一条「命令 → presenter 方法」的登记项。"""

    #: 命令路径（= 槽 id = ``shortcuts.ini`` 的键）。
    command: str
    #: 一句话说明（``keymap.slots`` / 未来的设置页展示用）。
    help: str
    #: 兜底键位：配置文件里没有这个命令时才生效。
    default_keys: tuple[str, ...]
    #: 目标子 presenter 的 :attr:`~albuswall.ui.presenter.base.Presenter.name`；
    #: :data:`CURRENT_ASSET` 表示「详情页优先、否则网格」。
    target: str
    #: 目标 presenter 上的公开方法名（零参、返回 ``bool``）。
    method: str

    def handler(self, container: Container) -> Callable[[], bool]:
        """构造处理函数（捕获容器，**不**捕获 presenter 实例）。"""
        if self.target == CURRENT_ASSET:
            return lambda: _call_on(_asset_presenter(container), self.method)
        return lambda: _call_on(_child(container, self.target), self.method)


#: 当前界面登记的全部快捷键槽（与 ``shortcuts.ini`` 的 18 条命令对位：
#: 12 条有默认键位，6 条默认解绑 ``= []``）。
UI_SLOTS: tuple[_SlotSpec, ...] = (
    _SlotSpec(
        "app.quit", "Quit albusWall", ("Ctrl+Q",),
        "window", "quit",
    ),
    _SlotSpec(
        "app.settings", "Open the settings page", ("Ctrl+,",),
        "window", "open_settings",
    ),
    _SlotSpec(
        "window.fullscreen", "Toggle fullscreen", ("F11", "Ctrl+Shift+F"),
        "window", "toggle_fullscreen",
    ),
    _SlotSpec(
        "window.close", "Close the current layer or the window", ("Ctrl+W",),
        "window", "close_layer",
    ),
    _SlotSpec(
        "window.album", "Show / hide the album page", (),
        "window", "toggle_album",
    ),
    _SlotSpec(
        "window.source", "Show / hide the import-source page", (),
        "window", "toggle_source",
    ),
    _SlotSpec(
        "view.zoom_in", "Zoom the detail image in", ("Ctrl++", "Ctrl+="),
        "viewer", "zoom_in",
    ),
    _SlotSpec(
        "view.zoom_out", "Zoom the detail image out", ("Ctrl+-",),
        "viewer", "zoom_out",
    ),
    _SlotSpec(
        "view.reset", "Fit the detail image to the window", ("Ctrl+0",),
        "viewer", "reset_zoom",
    ),
    _SlotSpec(
        "viewer.next", "Show the next asset in the detail page", (),
        "viewer", "next_asset",
    ),
    _SlotSpec(
        "viewer.prev", "Show the previous asset in the detail page", (),
        "viewer", "previous_asset",
    ),
    _SlotSpec(
        "content.search", "Focus the search bar", ("Ctrl+F",),
        "search", "focus_search",
    ),
    _SlotSpec(
        "content.preview", "Quick-preview the current grid asset", (),
        "content", "preview_current",
    ),
    _SlotSpec(
        "album.new", "Create a new album", ("Ctrl+N",),
        "album", "create_album",
    ),
    _SlotSpec(
        "album.rename", "Rename the current album", ("F2",),
        "album", "rename_current",
    ),
    _SlotSpec(
        "asset.favorite", "Toggle favorite for the current asset", ("Ctrl+D",),
        CURRENT_ASSET, "toggle_favorite",
    ),
    _SlotSpec(
        "asset.restore", "Restore the current asset from the trash", (),
        CURRENT_ASSET, "restore_current",
    ),
    _SlotSpec(
        "asset.delete", "Move the current asset to the trash", ("Delete",),
        CURRENT_ASSET, "trash_current",
    ),
)


# ======================================================================
# 登记 / 装载
# ======================================================================

def shortcut_registry_of(container: Container) -> Any:
    """容器里的快捷键注册表；容器没有时退回进程级单例。"""
    try:
        registry = container.get(SHORTCUTS_KEY)
    except Exception:  # noqa: BLE001 - 取不到就用默认注册表
        logger.exception("shortcuts: cannot resolve the shortcut registry")
        registry = None
    return registry if registry is not None else get_default_registry()


def install_ui_shortcuts(
        container: Container,
        *,
        registry: ShortcutRegistry | None = None,
        load: bool = True,
        strict: bool = False,
) -> Any:
    """把当前界面的动作登记成快捷键槽，并按需载入键位表。

    幂等：重复调用用 ``replace=True`` 覆盖同名槽（处理函数换成捕获
    ``container`` 的新闭包），不会抛 ``DuplicateShortcutSlot``。

    Args:
        container: 依赖容器；处理函数在触发时才从它取 presenter。
        registry: 目标注册表；默认取容器里的 ``shortcuts``，没有则用进程级
            单例（:func:`get_default_registry`）。
        load: 登记完是否立刻 ``load_shortcuts()``（整表重读配置文件）。
            单测可以直接给 ``False``，自己挑配置来源。
        strict: 传给 ``load_shortcuts``：``False`` 时配置里的坏条目只记
            warning 不抛（界面启动路径用这个）。

    Returns:
        实际登记用的注册表。
    """
    target = registry if registry is not None else shortcut_registry_of(container)

    for spec in UI_SLOTS:
        target.register_slot(
            ShortcutSlot(
                id=spec.command,
                handler=spec.handler(container),
                help=spec.help,
                default_keys=spec.default_keys,
            ),
            replace=True,
        )

    if load:
        try:
            load_shortcuts(registry=target, strict=strict)
        except Exception:  # noqa: BLE001 - 配置坏了不能拦住界面启动
            logger.exception(
                "shortcuts: loading the shortcut table failed; "
                "falling back to the slots' default keys")

    logger.debug(
        "shortcuts: %d UI slots registered (%s loaded)",
        len(UI_SLOTS), "table" if load else "defaults only")
    return target
