"""内置组件树（静态数据，Qt-free）。

``COMPONENTS`` 是模块级常量；插件需要新分组时导入期调用
:func:`register_component`，而不是在动作注册时自动补父节点。
"""
from __future__ import annotations

from collections.abc import Iterator

from .model import Component

__all__ = [
    "COMPONENTS", "COMPONENT_BY_ID", "clear_components",
    "component_children", "component_path", "get_component", "iter_components",
    "register_component",
]

BUILTIN_COMPONENTS: tuple[Component, ...] = (
    Component("global", "Global", None, "在任何界面（含输入框）里都生效。", 0),
    Component("window", "Main window", None, "窗口级动作：浮层、标题栏、窗口状态。", 10),
    Component("window.titlebar", "Title bar", "window", "搜索、更多菜单与标题栏按钮。", 10),
    Component("window.titlebar.search", "Search bar", "window.titlebar", "浮动搜索栏。", 10),
    Component("window.library", "Library", "window",
              "移动 / 删除 / 剪贴板 / 撤销等资产库动作。", 15),
    Component("window.content", "Asset grid", "window", "缩略图网格。", 20),
    Component("window.album", "Album list", "window", "相册浮层。", 30),
    Component("window.source", "Import sources", "window", "导入来源浮层。", 40),
    Component("window.preview", "Quick preview", "window",
              "快速预览 / 幻灯片 / 播放的显式模式。", 45),
    Component("window.detail", "Detail view", "window", "图片详情浮层。", 50),
    Component("window.detail.tagbar", "Tag bar", "window.detail", "详情页的标签栏。", 10),
    Component("window.setting", "Settings", "window", "设置浮层。", 60),
    Component("window.setting.keybindings", "Key bindings page", "window.setting",
              "按键映射设置页。", 10),
    Component("ui", "Focus & activation", None, "焦点循环、回车激活等键盘原语。", 70),
    Component("app", "Miscellaneous", None, "不属于具体组件的插件动作。", 90),
)

COMPONENTS: tuple[Component, ...] = BUILTIN_COMPONENTS
COMPONENT_BY_ID: dict[str, Component] = {c.id: c for c in COMPONENTS}


def get_component(component_id: str) -> Component | None:
    return COMPONENT_BY_ID.get(str(component_id or ""))


def component_path(component_id: str) -> tuple[Component, ...]:
    """从顶层到 ``component_id`` 的组件链（未知 id 返回空元组）。"""
    chain: list[Component] = []
    node = get_component(component_id)
    while node is not None:
        chain.append(node)
        node = get_component(node.parent) if node.parent else None
    chain.reverse()
    return tuple(chain)


def component_children(parent: str | None) -> tuple[Component, ...]:
    return tuple(sorted(
        (c for c in COMPONENTS if c.parent == parent),
        key=lambda c: (c.order, c.id),
    ))


def iter_components() -> Iterator[Component]:
    """深度优先遍历（父组件先于子组件）。"""
    stack = list(reversed(component_children(None)))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(component_children(node.id)))


def register_component(component: Component) -> Component:
    """追加一个组件（导入期调用；重复 id 抛 ``ValueError``）。"""
    global COMPONENTS, COMPONENT_BY_ID
    if component.id in COMPONENT_BY_ID:
        raise ValueError(f"duplicate component id: {component.id!r}")
    COMPONENTS = (*COMPONENTS, component)
    COMPONENT_BY_ID = {c.id: c for c in COMPONENTS}
    return component


def clear_components() -> None:
    """测试用：恢复到内置组件树。"""
    global COMPONENTS, COMPONENT_BY_ID
    COMPONENTS = BUILTIN_COMPONENTS
    COMPONENT_BY_ID = {c.id: c for c in COMPONENTS}
