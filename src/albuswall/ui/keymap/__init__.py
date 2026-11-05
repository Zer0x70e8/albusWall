#
"""``albuswall.ui.keymap`` —— 快捷键：注册层 + Qt 事件层。

``albuswall.command`` 有意把键盘快捷键留给界面层（见 ``command/registry.py``
的 ``TODO(ui)``）：命令知道「叫什么」，不知道「哪个键能触发我」。这个子包
做两件事：**注册**（谁能被触发）与**接管按键**（按下去要落到谁）：

====================================  ======================================
模块                                  职责
====================================  ======================================
``sequence``                          Qt 键位字符串的解析 / 规范化 / 分类
``keys``                              按键身份枚举：一个物理键的全部写法
                                      (:class:`KeyAlias`；``Shift+Tab`` ==
                                      ``Shift+Backtab`` == ``Backtab``)
``model``                             快捷键槽 / 绑定记录
                                      (:class:`ShortcutSlot` /
                                      :class:`ShortcutBinding`)
``registry``                          槽注册表 + 双向一对多绑定表
``config``                            默认表（resources）+ 用户表（持久化）
``qt_binder``                         ``QKeyEvent`` → 槽的事件过滤器
                                      (:class:`ShortcutBinder`)
``errors``                            本层异常
====================================  ======================================

键位支持三种形态（Qt PortableText）：

- **单按键**：``F5`` / ``Space``
- **修饰键组合**：``Ctrl+N`` / ``Ctrl+Shift+P``
- **非修饰键和弦**：``G, G`` / ``Ctrl+K, Ctrl+C``（逗号分隔和弦步）

绑定表是**单独一个配置文件**，每次都整表重新加载：

- 默认表：``resources/config/shortcuts.ini``（随包分发，开箱即用）
- 用户表：``<user_config_dir>/shortcuts.ini``（只存与默认不同的条目）

最小用法：

.. code-block:: python

    from albuswall.ui.keymap import ShortcutRegistry, ShortcutConfig

    shortcuts = ShortcutRegistry()

    @shortcuts.slot("view.fullscreen", help_="全屏", keys=["F11"])
    def toggle_fullscreen() -> None: ...

    # 带固定实参的槽：触发时调用 open_album(3, mode="fit")
    @shortcuts.slot("album.open", keys="Alt+1", args=(3,), kwargs={"mode": "fit"})
    def open_album(index: int, *, mode: str = "fit") -> None: ...

    # 一个命令挂多个键（配置文件里的写法）
    shortcuts.bind_command("view.fullscreen", ["F11", "Ctrl+Shift+F"])
    # 一个键挂多个命令（一对多）
    shortcuts.bind("Ctrl+W", "window.close", "viewer.close")

    shortcuts.load(ShortcutConfig())   # 每次启动都从配置文件重读
    shortcuts.persist()                # 把与默认不同的条目写回用户表

    shortcuts.trigger("Alt+1")         # 按绑定表调用槽（不涉及 Qt 事件）

每条绑定记录（``命令 = [快捷键, 位置参数, 关键字参数]``，见
:mod:`albuswall.ui.keymap.config`）里可以带固定实参；槽本身也能在装饰器里
声明兜底实参。触发路径统一走 :meth:`ShortcutRegistry.invoke`，它会用
「配置实参 + 显式实参」调用处理函数。

``qt_binder`` 是唯一 import ``QtWidgets`` 的模块（其余模块只碰
``QtGui.QKeySequence``）；它**只认识键盘事件与绑定表**，不认识 presenter /
视图，是「按键 → 命令」的最后一段。焦点链、Esc 层级、回车→空格这些语义
都不归它，见 :mod:`albuswall.ui.keymap.qt_binder` 的模块注释。

.. code-block:: python

    from albuswall.ui.keymap import install_shortcut_binder

    install_shortcut_binder(QApplication.instance(), registry, owner=main_window)
    # 之后按 F11 / Ctrl+N 就会走 registry.trigger(...)
"""

from __future__ import annotations

from albuswall.common.keymap import ShortcutRecord

from .config import (
    DEFAULT_FILE_NAME,
    DEFAULT_SECTION,
    ShortcutConfig,
    default_config_path,
    user_config_path,
)
from .errors import (
    DuplicateShortcutSlot,
    InvalidKeySequence,
    KeymapError,
    ShortcutConfigError,
)
from .keys import (
    NAVIGATION_KEYS,
    WIDGET_KEYS,
    KeyAlias,
    is_navigation_step,
    is_widget_step,
    key_alias_of,
    sequence_identity,
    split_step,
    step_identity,
)
from .model import ShortcutBinding, ShortcutSlot
from .qt_binder import (
    DEFAULT_CHORD_TIMEOUT_MS,
    ShortcutBinder,
    install_shortcut_binder,
    uninstall_shortcut_binder,
)
from .registry import (
    KeyInput,
    ShortcutRegistry,
    bind,
    bind_command,
    get_default_registry,
    load_shortcuts,
    registry_shortcut_registry,
    shortcut_slot,
)
from .sequence import (
    CHORD_SEPARATOR,
    MAX_CHORDS,
    MODIFIERS,
    KeySequence,
    ShortcutKind,
    canonical_sequence,
    format_sequences,
    parse_sequence,
    parse_sequences,
)

__all__ = [
    "CHORD_SEPARATOR",
    "DEFAULT_CHORD_TIMEOUT_MS",
    "DEFAULT_FILE_NAME",
    "DEFAULT_SECTION",
    "MAX_CHORDS",
    "MODIFIERS",
    "NAVIGATION_KEYS",
    "WIDGET_KEYS",
    "DuplicateShortcutSlot",
    "InvalidKeySequence",
    "KeyAlias",
    "KeyInput",
    "KeySequence",
    "KeymapError",
    "ShortcutBinder",
    "ShortcutBinding",
    "ShortcutConfig",
    "ShortcutConfigError",
    "ShortcutKind",
    "ShortcutRecord",
    "ShortcutRegistry",
    "ShortcutSlot",
    "bind",
    "bind_command",
    "canonical_sequence",
    "default_config_path",
    "format_sequences",
    "get_default_registry",
    "install_shortcut_binder",
    "is_navigation_step",
    "is_widget_step",
    "key_alias_of",
    "load_shortcuts",
    "parse_sequence",
    "parse_sequences",
    "registry_shortcut_registry",
    "sequence_identity",
    "shortcut_slot",
    "split_step",
    "step_identity",
    "uninstall_shortcut_binder",
    "user_config_path",
]
