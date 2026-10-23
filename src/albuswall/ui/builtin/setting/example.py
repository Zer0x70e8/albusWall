#
"""扩展示例：往设置页里加一个「自定义页」。

设置页的内容有四种扩展方式，渲染路径完全一样（都是懒加载的可折叠分组 /
同一套「一行」）：

1. **配置项**：``configue.setting_schema`` 的登记表（``albusctl setting.schema``）；
2. **改已有项**：``overrides.py`` 的 :class:`~albuswall.ui.builtin.setting.overrides.ItemPatch`
   —— 换控件、给动态候选值、一个值写多个配置节点；
3. **加自定义行**：``extras.py`` 的 :class:`~albuswall.ui.builtin.setting.extras.ExtraSpec`
   —— 分组里的动作按钮 / 说明 / 插件控件，没有配置节点；
4. **加自定义页**：``pages.py`` 的 :class:`~albuswall.ui.builtin.setting.pages.PageSpec`。

本文件每种都演示一遍。它**故意不在包的 ``__init__`` 里导入**：示例不该污染
真实的设置页。想试的话：

.. code-block:: python

    from albuswall.ui.builtin.setting import example   # 导入即登记
    # 然后重新打开设置页（或点右上角的刷新）
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .extras import extra
from .overrides import item_patch
from .pages import page

__all__ = ["build_diagnostics"]


def build_diagnostics(host: QWidget) -> None:
    """往内容容器里塞一个简单的诊断页。"""
    lines: list[str] = []

    try:
        from albuswall.__about__ import __title__, __version__

        lines.append(f"{__title__} {__version__}")
    except Exception:  # noqa: BLE001
        lines.append("version: unknown")

    try:
        from albuswall.services.command import list_commands

        commands = list_commands()
        lines.append(f"IPC commands: {len(commands)}")
        lines.extend(f"  · {name}" for name in commands)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"IPC commands unavailable: {exc}")

    layout = QVBoxLayout()
    layout.setContentsMargins(4, 2, 4, 4)
    layout.setSpacing(2)
    for line in lines:
        label = QLabel(line, host)
        label.setObjectName("SettingDiagnosticsLine")
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(label)

    container = QWidget(host)
    container.setLayout(layout)
    outer = host.layout()
    if outer is None:  # 被独立调用时自己补一个布局
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(container)


#: 演示「改已有项」：给缩略图批量加一句提示（不动 services 层的登记表）
@item_patch(
    "thumbnails.batch_size",
    hint="Example patch from example.py",
)
def _patch_batch_size(value: Any, item: dict[str, Any]) -> None:
    """纯展示补丁：返回 None 表示「照常写回本项」。"""
    return None


#: 演示「加自定义行」：往缩略图分组末尾插一条按钮行（示例，不参与真实 UI）
@extra(
    "example.refresh",
    group="thumbnails",
    title="Refresh thumbnail cache",
    order=900,
    description="Example action row registered by example.py.",
    button="Refresh",
)
def _example_action(host: QWidget) -> Any:
    """按钮行不需要 build：返回 None 即可（这里显式写出来当示例）。"""
    return None


@page(
    "diagnostics",
    title="Diagnostics",
    order=950,
    description="Runtime introspection (example: custom page registration).",
)
def _register_diagnostics(host: QWidget) -> Any:
    build_diagnostics(host)
