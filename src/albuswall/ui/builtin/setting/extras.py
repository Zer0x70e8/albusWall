#
"""分组里的「非配置行」注册表。

``configue.setting_schema`` 描述的是**配置节点**：每个叶子都绑定一个
``[section]key``，有值、能写回。但设置页里还有些东西既没有配置节点、
也不该藏在 ``main.py`` 的 ``addWidget`` 里，例如「编辑主题」这种**动作**
行。本模块给它们一个和配置项平级的注册入口：

    @extra("theme_details.edit", group="appearance.theme_details",
           title="Edit", button="Edit", order=40)
    def build_edit(host):
        ...

两个层次故意分开：

* **注册**（本模块）只操作普通数据，不 import Qt —— 插件 / 无 UI 环境也能
  声明自己的行；
* **渲染**（``rows.ExtraRow``）才把 :class:`ExtraSpec` 变成控件。

渲染顺序由 :mod:`~albuswall.ui.builtin.setting.layout` 统一决定：叶子、
自定义行、子分组都带 ``order``，同层按 ``order`` 从小到大混排。自定义行的
``order`` 默认是 500（比配置项的 0 大、比「关于」页的 900 小），也就是
「不写 ``order`` 就排在已有内容之后」。

用法（三种形态）::

    from albuswall.ui.builtin.setting.extras import ExtraSpec, extra

    # 1. 纯说明行：只有标题 + 描述
    extra("notes.privacy", group="advanced.ipc", title="Privacy")(lambda host: None)

    # 2. 动作行：标题 + 按钮（``clicked`` 留空就是占位按钮）
    register_extra(ExtraSpec(
        "theme_details.edit",
        group="appearance.theme_details",
        title="Edit", button="Edit", order=40,
    ))

    # 3. 完全自定义控件：``build`` 返回的控件放在行尾（可以和按钮共存）
    @extra("diagnostics.snapshot", group="advanced.task", title="Snapshot")
    def _snapshot(host):
        return QLabel("…", host)
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ExtraSpec",
    "extra",
    "extras_of",
    "iter_extras",
    "register_extra",
    "unregister_extra",
]

#: 自定义行构建回调：拿到父控件，返回一个控件（或 ``None`` 表示没有控件）
ExtraBuilder = Callable[[Any], Any]


@dataclass(frozen=True)
class ExtraSpec:
    """一条「分组内自定义行」的注册项。

    Args:
        id: 唯一 id（日志 / 测试用它）。
        group: 目标分组的 qualname，如 ``appearance.theme_details``。
        title: 行标题（左侧文字）。
        order: 同层排序权重，与配置项的 ``order`` 同尺度。
        description: 行下方的一句话说明（英文）。
        button: 按钮文字；为空表示这一行没有按钮。
        clicked: 按钮回调；``None`` = 占位按钮（点了不做任何事）。
        build: 自定义控件工厂；给了它就不再画默认的「标题 + 按钮」布局，
            而是把返回的控件放到行尾。
    """

    id: str
    group: str
    title: str
    order: int = 500
    description: str = ""
    button: str = ""
    clicked: Callable[[], Any] | None = field(default=None, compare=False)
    build: ExtraBuilder | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        """跨注册表唯一的键（和配置项的 key 区分开）。"""
        return f"extra:{self.id}"


_EXTRAS: dict[str, ExtraSpec] = {}


def register_extra(spec: ExtraSpec, *, replace: bool = False) -> ExtraSpec:
    """登记一条自定义行。

    重复 id 默认抛异常（与 ``command`` / ``page`` / ``item_patch`` 的约定
    一致：静默覆盖会让「谁赢了」变成 import 顺序的副产品）。
    """
    if not spec.id:
        raise ValueError("setting extra id must be a non-empty string")
    if not spec.group:
        raise ValueError(f"setting extra {spec.id!r}: group is required")
    if not spec.title:
        raise ValueError(f"setting extra {spec.id!r}: title is required")
    if not replace and spec.id in _EXTRAS:
        raise ValueError(f"duplicate setting extra: {spec.id!r}")
    _EXTRAS[spec.id] = spec
    return spec


def unregister_extra(extra_id: str) -> ExtraSpec | None:
    """移除一条注册（测试用）。"""
    return _EXTRAS.pop(extra_id, None)


def iter_extras() -> Iterator[ExtraSpec]:
    """按 ``order`` / ``id`` 排好序的全部注册项。"""
    yield from sorted(_EXTRAS.values(), key=lambda spec: (spec.order, spec.id))


def extras_of(group_key: str) -> tuple[ExtraSpec, ...]:
    """某个分组（qualname）下的自定义行，已按 ``order`` 排好。"""
    if not group_key:
        return ()
    return tuple(
        spec for spec in iter_extras() if spec.group == group_key
    )


def extra(
        extra_id: str,
        *,
        group: str,
        title: str | None = None,
        order: int = 500,
        description: str = "",
        button: str = "",
        clicked: Callable[[], Any] | None = None,
        replace: bool = False,
) -> Callable[[ExtraBuilder], ExtraBuilder]:
    """装饰器形态的 :func:`register_extra`（被修饰函数作为 ``build``）。"""

    def deco(build: ExtraBuilder) -> ExtraBuilder:
        register_extra(
            ExtraSpec(
                id=extra_id,
                group=group,
                title=title or extra_id,
                order=order,
                description=description,
                button=button,
                clicked=clicked,
                build=build,
            ),
            replace=replace,
        )
        return build

    return deco
