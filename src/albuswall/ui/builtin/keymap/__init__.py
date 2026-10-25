#
"""可按键映射的配置（键位表）。

分层（**registry 不依赖 Qt**，插件可以在导入期安全登记；其余模块按需导入）：

======================  ================================================
模块                     职责
======================  ================================================
``registry``            动作登记表：``KeyAction`` / ``register_action``
``actions``             内置动作（窗口 / 搜索 / 详情页）
``keys``                按键文本 ↔ 键盘事件的唯一解释层（PortableText）
``state``               ``dynamic.keymap`` 子配置文件（JSON）的字段声明
``store``               生效键位的读写门面（设置页 / 分发共用）
``manager``             ``QApplication`` 事件过滤器，按键 → 动作
``navigation``          F6 焦点区域循环 / 回车激活按钮等「键盘导航」原语
``page``                设置页里的「按键映射」视图（搜索 / 编辑 / 重置）
======================  ================================================

键盘操作的总原则（纯键盘可以走完所有流程）：

* **全局键**（打开浮层、翻页、Esc 逐层收起、F6 焦点循环…）全部登记在
  ``actions``，用户可在设置页改键；
* **控件内部键**（输入文字、列表方向键、下拉框、树）保持 Qt 原生行为，
  全局分发层只挑自己登记的组合，不抢控件自己的按键；
* 列表 / 网格里 ``Enter`` 交给控件激活（网格用 ``unit_activated`` 信号），
  ``Enter`` 在按钮上则由 ``ui.activate`` 补一次点击（Qt 原生只认 Space）。

最小用法（插件新增一个可映射功能）::

    from albuswall.ui.builtin.keymap import KeyAction, register_action

    register_action(KeyAction(
        id="demo.hello",
        label="Say hello",
        default_keys="Ctrl+Alt+H",
        handler=lambda ctx: print("hello", ctx.window),
    ))

配置落在 ``<config>/keymap.json``（``[files] keymap`` 控制文件名）：

.. code-block:: json

    {
      "enabled": true,
      "bindings": {"viewer.next": "Ctrl+Right"}
    }

设置页的入口是 ``page.py``：导入即往设置页注册一个「Key bindings」页，
列出全部「动作 ↔ 按键」，带搜索、逐行修改与恢复默认。
"""

from .registry import (
    GROUP_ORDER,
    KeyAction,
    clear_actions,
    get_action,
    group_rank,
    iter_actions,
    register_action,
    unregister_action,
)

__all__ = [
    "GROUP_ORDER",
    "KeyAction",
    "clear_actions",
    "get_action",
    "group_rank",
    "iter_actions",
    "register_action",
    "unregister_action",
]
