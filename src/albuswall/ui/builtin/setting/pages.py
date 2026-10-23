#
"""设置页「自定义页」注册表。

设置页的内容有两个来源，UI 层把它们当成同一种东西（一个可折叠的懒加载
分组）：

1. **配置分组** —— ``configue.setting_schema`` 里的登记表，也就是
   ``albusctl setting.schema '{"view":"tree"}'`` 输出的那棵树；
2. **自定义页** —— 本模块的 :class:`PageSpec`：不绑定任何配置节点的页面，
   比如「关于」「致谢」「诊断信息」。

这样「关于」页就从「硬编码在 ``main.py`` 里的一次 ``addWidget``」变成了
一条注册项：谁想加一个页面，就写一个 ``@page(...)`` 函数，UI 自动渲染，
导航按钮也自动出现。

用法::

    from albuswall.ui.builtin.setting.pages import page

    @page("about", title="About", order=900, description="Version info")
    def build_about(host):
        QLabel("...", host).setParent(host)

``build`` 只会被调用一次，而且是在分组**第一次展开**的时候
（见 :class:`~albuswall.ui.builtin.setting.lazy.LazyGroupBox`）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    from PySide6.QtWidgets import QWidget

__all__ = [
    "PageSpec",
    "iter_pages",
    "page",
    "register_page",
    "unregister_page",
]

#: 页面内容构建回调：拿到内容容器，往里塞控件
PageBuilder = Callable[["QWidget"], Any]


@dataclass(frozen=True)
class PageSpec:
    """一条自定义页注册项。

    Args:
        id: 唯一 id（导航按钮的 objectName、日志都用它）。
        title: 分组标题。
        order: 排序权重（配置分组用 10/20/…/90，自定义页一般往后放）。
        description: 展开后显示在内容顶部的一句话。
        build: 内容构建回调；``None`` 表示先占个标题、内容留空。
    """

    id: str
    title: str
    order: int = 500
    description: str = ""
    build: PageBuilder | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        """跨来源唯一的键（配置分组用 ``schema:<group key>``）。"""
        return f"page:{self.id}"


_PAGES: dict[str, PageSpec] = {}


def register_page(spec: PageSpec, *, replace: bool = False) -> PageSpec:
    """登记一个自定义页。

    重复 id 默认抛异常（与 ``command`` / ``PluginManager`` 的约定一致：
    静默覆盖会让「谁赢了」变成 import 顺序的副产品）。
    """
    if not spec.id:
        raise ValueError("setting page id must be a non-empty string")
    if not spec.title:
        raise ValueError(f"setting page {spec.id!r}: title is required")
    if not replace and spec.id in _PAGES:
        raise ValueError(f"duplicate setting page: {spec.id!r}")
    _PAGES[spec.id] = spec
    return spec


def unregister_page(page_id: str) -> PageSpec | None:
    """移除一条注册（测试用；正常运行时不需要）。"""
    return _PAGES.pop(page_id, None)


def iter_pages() -> tuple[PageSpec, ...]:
    """按 ``order`` / ``id`` 排好序的注册项。"""
    return tuple(sorted(_PAGES.values(), key=lambda spec: (spec.order, spec.id)))


def page(
        page_id: str,
        *,
        title: str | None = None,
        order: int = 500,
        description: str = "",
        replace: bool = False,
) -> Callable[[PageBuilder], PageBuilder]:
    """装饰器形态的 :func:`register_page`。

    ::

        @page("about", title="About", order=900)
        def build_about(host): ...
    """

    def deco(build: PageBuilder) -> PageBuilder:
        register_page(
            PageSpec(
                id=page_id,
                title=title or page_id,
                order=order,
                description=description,
                build=build,
            ),
            replace=replace,
        )
        return build

    return deco
