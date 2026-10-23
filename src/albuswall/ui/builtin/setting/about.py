#
"""「关于」页 —— 通过 :mod:`~albuswall.ui.builtin.setting.pages` 注册。

以前 ``About`` 是一个被 ``main.py`` 手工 ``addWidget`` 的折叠框；现在它只是一条
**注册项**，和配置分组走完全相同的渲染路径：

* 注册一个 :class:`~albuswall.ui.builtin.setting.pages.PageSpec`；
* 内容在建组时才创建（``build`` 回调），第一次展开之前一个控件都没有；
* 导航按钮、排序、折叠样式全部由设置页统一处理。

想再加一个「感谢名单」「诊断信息」之类的页，照抄本文件的 ``@page`` 即可。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from albuswall.__about__ import (
    __author__,
    __title__,
    __version__,
)
from albuswall.__about__ import (
    __doc__ as __about_doc__,
)

from .pages import page
from .translation import schema_tr

__all__ = ["About", "about_summary", "build_about"]

#: 排序权重：配置分组最多用到 90，「关于」放到最后
ABOUT_ORDER = 900


def about_summary() -> dict[str, str]:
    """关于信息（也给测试 / 其它页面复用）。"""
    return {
        "title": str(__title__),
        "version": str(__version__),
        "author": str(__author__),
        "description": (__about_doc__ or "").strip(),
    }


def build_about(host: QWidget) -> None:
    """在 ``host`` 里画出关于信息（懒执行：第一次展开时才跑）。"""
    info = about_summary()

    layout = QVBoxLayout()
    layout.setContentsMargins(4, 2, 4, 4)
    layout.setSpacing(10)

    layout.addWidget(_paragraph(host, schema_tr(info["description"])))

    grid = QGridLayout()
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(4)
    for row_index, (name, value) in enumerate(
            (("Title", info["title"]),
             ("Version", info["version"]),
             ("Author", info["author"]))
    ):
        key = QLabel(schema_tr(name), host)
        key.setObjectName("SettingAboutKey")
        value_label = QLabel(value, host)
        value_label.setObjectName("SettingAboutValue")
        value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        grid.addWidget(key, row_index, 0)
        grid.addWidget(value_label, row_index, 1)
    grid.setColumnStretch(1, 1)
    layout.addLayout(grid)

    layout.addWidget(_paragraph(host, schema_tr("Thanks for using it.")))

    container = QWidget(host)
    container.setLayout(layout)
    outer = host.layout()
    if outer is None:  # 被独立使用时自己补一个布局
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(container)


def _paragraph(host: QWidget, text: str) -> QLabel:
    label = QLabel(text, host)
    label.setObjectName("SettingAboutText")
    label.setWordWrap(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    return label


@page(
    "about",
    title="About",
    order=ABOUT_ORDER,
    description="Version, author and a short description of the project.",
)
def _register_about(host: QWidget) -> Any:
    """注册项本体（返回值不重要，UI 只关心它往 host 里塞了什么）。"""
    build_about(host)


class About(QWidget):
    """兼容旧用法的「关于」页（内容在构造时就建好）。

    设置页自己走注册表，不需要这个类；保留它是为了不打断外部
    ``from .about import About`` 的写法。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingAbout")
        build_about(self)
