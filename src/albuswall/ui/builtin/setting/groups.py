#
"""把 ``setting.schema`` 的分组 dict 渲染成控件。

渲染规则（一条就够：见到 dict 就建一个懒加载折叠框）::

    group dict ──► LazyGroupBox（展开时才建）
                     ├─ items  ──► SettingRow（标签 + 编辑器）
                     ├─ extras ──► ExtraRow（注册的自定义行，见 extras.py）
                     └─ groups ──► LazyGroupBox（递归，同样懒加载）

三者的**上下顺序**由 :mod:`~albuswall.ui.builtin.setting.layout` 决定：同层
所有条目按 ``order`` 从小到大混排，所以「第一项是叶子、第二项是叶子、第三
项是子分组」这种布局只靠登记时的 ``order`` 表达，不需要在渲染层特判任何
分组名。

所以打开设置页时只创建了「标题」：66 个叶子、16 个分组在点击之前一个控件都
不存在。分组说明作为 tooltip 常驻，展开后也会显示在内容顶部，不额外占地方。
"""

from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtWidgets import QVBoxLayout, QWidget

from .controller import SettingController
from .layout import Entry, iter_entries
from .lazy import LazyGroupBox
from .rows import ExtraRow, SettingRow
from .translation import schema_tr

__all__ = ["build_group_details", "make_group_box"]

#: 折叠动画时长（毫秒）；与「关于」页保持一致
ANIM_TIME = 220


def _row_animation():  # pragma: no cover - 纯导入辅助
    try:
        from ..anims.collapse_animation import HeightSlideAnimation
    except ImportError:
        from albuswall.ui.builtin.anims.collapse_animation import (
            HeightSlideAnimation,
        )
    return HeightSlideAnimation(ANIM_TIME)


def make_group_box(
        group: dict[str, Any],
        controller: SettingController,
        parent: QWidget | None = None,
) -> LazyGroupBox:
    """为一个分组 dict 造懒加载折叠框（内容还没建）。"""
    return LazyGroupBox(
        schema_tr(group.get("title") or group.get("id") or "?"),
        builder=partial(build_group_details, group=group, controller=controller),
        parent=parent,
        animation=_row_animation(),
        collapsed=True,
        description=schema_tr(group.get("description") or "").strip(),
        object_name="SettingGroupBox",
    )


def build_group_details(
        host: QWidget,
        *,
        group: dict[str, Any],
        controller: SettingController,
) -> None:
    """在 ``host`` 里填一个分组的内容（叶子 / 自定义行 / 子分组按 order 混排）。

    ``host`` 的布局由 ``LazyGroupBox`` 提供（说明文字 + 内容 + 尾部 stretch），
    这里沿用同一个布局，别自己再建一个，也别再加 stretch。
    """
    layout = host.layout()
    if layout is None:  # 被独立调用时的兜底
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch()
    layout.setSpacing(2)

    for entry in iter_entries(group):
        widget = _build_entry(entry, controller, host)
        if widget is not None:
            layout.addWidget(widget)


def _build_entry(
        entry: Entry,
        controller: SettingController,
        host: QWidget,
) -> QWidget | None:
    """把一条 :class:`~albuswall.ui.builtin.setting.layout.Entry` 变成控件。"""
    if entry.is_item:
        return SettingRow(entry.payload, controller, host)
    if entry.is_extra:
        return ExtraRow(entry.payload, host)
    if entry.is_group:
        return make_group_box(entry.payload, controller, host)
    return None
