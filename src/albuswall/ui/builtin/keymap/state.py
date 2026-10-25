#
"""键位配置的落盘形态（``dynamic.keymap`` 子树）。

这个子树是一份**子配置文件**（默认 ``<config>/keymap.json``），登记点在
``albuswall.ui.builtin.config.static``：

.. code-block:: python

    register_dynamic_state(
        "keymap",
        "albuswall.ui.builtin.keymap.state:KeymapState",
        ("path", "config"),
        WindowPresenterConfs.keymap_file_name,
        autosave=True,
    )

和 ``dynamic.window`` 完全同一套机制：导入期只登记、访问期才 import 本模块
并读盘，UI 实现（设置页 / 按键分发）晚于配置加载也没关系。

文件只保存**用户覆盖**，不保存默认值：

.. code-block:: json

    {
      "enabled": true,
      "bindings": {
        "viewer.next": "Ctrl+Right",
        "window.source": ""
      }
    }

约定：

* ``bindings`` 的键是动作 id（见 ``registry``），值是 PortableText 按键；
* **空字符串表示显式解绑**（用户把某个默认键删掉了）；
* 不在 ``bindings`` 里的动作一律用 ``KeyAction.default_keys``，因此默认值
  可以随版本演进，不需要迁移旧文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from albuswall.configue import StateField, StatefulNamespace

__all__ = ["KeymapState"]


class KeymapState(StatefulNamespace):
    """``dynamic.keymap`` 的字段声明。"""

    #: 总开关；False 时所有按键映射都不响应
    enabled = StateField(bool, default=True)
    #: 动作 id -> PortableText 按键；缺省用动作自带的默认值
    bindings = StateField(dict, default=None)

    @property
    def path(self) -> Optional[Path]:
        """这份子配置文件的落盘路径（未绑定文件时为 None）。"""
        return self._ctx.path
