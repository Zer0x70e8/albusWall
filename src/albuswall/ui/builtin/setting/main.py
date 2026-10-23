#
"""设置页装配层：把「注册表」渲染成可折叠的懒加载界面。

内容只有一个来源规则：

    schema 分组（``albusctl setting.schema`` 那棵树）
        + 设置项补丁（``overrides.py``：把已有项改成下拉框 / 多键写回…）
        + 自定义行（``extras.py``：分组里的动作 / 说明 / 插件控件）
        + 自定义页注册表（``pages.py``：关于 / 诊断…）
                        ↓
        每个顶层条目 = 一个 LazyGroupBox（点开才建控件）
                        ↓
        左侧导航按钮 + 右侧内容，各自按 order 排序

一层里没有任何硬编码的分组名：「关于」页以前是这里手写的一个 ``addWidget``，
现在它只是 :mod:`~albuswall.ui.builtin.setting.pages` 里的一条注册项
（见 ``about.py`` 的 ``@page("about", ...)``）。

懒创建
======
打开设置页时只会建出「标题 + 说明」，叶子行、编辑器、子分组全都在第一次
展开时创建（:meth:`LazyGroupBox.ensure_built`）。66 个叶子全部实例化大约要
几十毫秒并占用大量内存，而用户通常只看其中一两个分组。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import about as _about  # noqa: F401  导入即登记「关于」页
from . import builtin_patches as _patches  # noqa: F401  导入即登记内置补丁
from .backend import SettingBackend
from .basic import Setting as _Setting
from .basic import insert_before_stretch
from .controller import SettingController
from .groups import ANIM_TIME, build_group_details
from .lazy import LazyGroupBox
from .pages import PageSpec, iter_pages
from .translation import schema_tr

try:
    from ..anims.collapse_animation import HeightSlideAnimation
except ImportError:  # pragma: no cover
    from albuswall.ui.builtin.anims.collapse_animation import HeightSlideAnimation

__all__ = ["Setting"]

#: 导航按钮 objectName 的后缀（主题 QSS 靠它命中）
JUMO_BUTTON_OBJ_NAME_SUFFIX = "JumpButton"


def _pascal(text: str) -> str:
    """``theme_completion`` / ``a.b`` → ``ThemeCompletion`` / ``AB``。"""
    parts: list[str] = []
    for chunk in str(text).replace(".", "_").replace(":", "_").split("_"):
        if chunk:
            parts.append(chunk[:1].upper() + chunk[1:])
    return "".join(parts) or "Section"


@dataclass
class _Section:
    """一个顶层条目：要么是配置分组，要么是自定义页。"""

    id: str
    title: str
    order: int = 0
    kind: str = "schema"          # "schema" | "page"
    group: dict[str, Any] = field(default_factory=dict)
    page: PageSpec | None = None
    description: str = ""

    @property
    def sort_key(self) -> tuple[int, str]:
        return (self.order, self.title.casefold())


class Setting(_Setting):
    """设置页（外壳见 ``basic.py``，数据见 ``backend.py``）。"""

    def __init__(self, parent=None, target=None, auto_setup=True):
        super().__init__(parent, target=target, auto_setup=auto_setup)
        self.setup_ui()

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def setup_ui(self) -> None:
        if not hasattr(self, "content_layout"):  # auto_setup=False 时补骨架
            self._setup_ui()
        self._controller = SettingController(SettingBackend(), self)
        self._section_boxes: list[LazyGroupBox] = []
        self._nav_buttons: list[QPushButton] = []
        self._auto_expanded = False

        self._build_header()

        # 两个 stretch 先占好位，章节用「插到 stretch 之前」的方式追加，
        # 这样 reload 时可以随意清空重建。
        self.content_layout.addStretch()
        self.nav_layout.addStretch()

        self._controller.changed.connect(self._on_changed)
        self._controller.failed.connect(self._on_failed)
        self._controller.applied.connect(self._on_values_applied)

        self.reload_schema()

    def _build_header(self) -> None:
        self.header = QWidget(self.content_widget)
        self.header.setObjectName("SettingHeader")

        layout = QHBoxLayout(self.header)
        layout.setContentsMargins(2, 2, 2, 8)
        layout.setSpacing(8)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)

        self.title_label = QLabel(self.tr("Settings"), self.header)
        self.title_label.setObjectName("SettingHeaderTitle")

        self.summary_label = QLabel("", self.header)
        self.summary_label.setObjectName("SettingHeaderSummary")
        self.summary_label.setWordWrap(True)

        self.status_label = QLabel("", self.header)
        self.status_label.setObjectName("SettingHeaderStatus")

        text_box.addWidget(self.title_label)
        text_box.addWidget(self.summary_label)
        text_box.addWidget(self.status_label)

        self.reload_button = QToolButton(self.header)
        self.reload_button.setObjectName("SettingReloadButton")
        self.reload_button.setText("\u21bb")  # ↻
        self.reload_button.setAutoRaise(True)
        self.reload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_button.setToolTip(
            self.tr("Reload the setting tree (setting.schema)")
        )
        self.reload_button.clicked.connect(lambda _checked=False: self.reload_schema())

        layout.addLayout(text_box, 1)
        layout.addWidget(self.reload_button, 0, Qt.AlignmentFlag.AlignTop)

        self.content_layout.addWidget(self.header)

    # ------------------------------------------------------------------ #
    # 数据 → 界面
    # ------------------------------------------------------------------ #
    def reload_schema(self) -> None:
        """重新读一遍设置树并重建章节（已在编辑的控件会被丢弃）。"""
        schema = self._controller.reload()
        self._rebuild_sections(schema)
        self._update_summary(schema)

    def _section_entries(self, schema: dict[str, Any]) -> list[_Section]:
        entries: list[_Section] = []

        for group in schema.get("groups") or []:
            if not isinstance(group, dict):
                continue
            key = str(group.get("key") or group.get("id") or "")
            entries.append(
                _Section(
                    id=key or "group",
                    title=str(group.get("title") or group.get("id") or key),
                    order=int(group.get("order") or 0),
                    kind="schema",
                    group=group,
                    description=str(group.get("description") or ""),
                )
            )

        for spec in iter_pages():
            entries.append(
                _Section(
                    id=spec.id,
                    title=spec.title,
                    order=spec.order,
                    kind="page",
                    page=spec,
                    description=spec.description,
                )
            )

        entries.sort(key=lambda section: section.sort_key)
        return entries

    def _rebuild_sections(self, schema: dict[str, Any]) -> None:
        self._clear_sections()

        for section in self._section_entries(schema):
            box = self._make_section_box(section)
            insert_before_stretch(self.content_layout, box)
            self._section_boxes.append(box)

            button_text = schema_tr(section.title)
            if section.kind == "schema":
                count = int(section.group.get("item_count") or 0)
                if count:
                    button_text = f"{button_text}  ({count})"
            button = self.add_nav_entry(
                button_text,
                lambda target=box: self.jump_to(target),
                f"Setting{_pascal(section.id)}{JUMO_BUTTON_OBJ_NAME_SUFFIX}",
            )
            self._nav_buttons.append(button)

        # 刷新时用户本来就在看着设置页：先摊开第一章，别让整页只剩标题
        if self.isVisible() and self._section_boxes:
            first = self._section_boxes[0]
            first.ensure_built()
            first.setChecked(True)
            self._auto_expanded = True

    def _make_section_box(self, section: _Section) -> LazyGroupBox:
        title = schema_tr(section.title)
        if section.kind == "page" and section.page is not None:
            object_name = "SettingPage"
            builder = self._page_builder(section.page)
        else:
            object_name = "SettingSection"
            group = section.group
            controller = self._controller

            def builder(host: QWidget, group=group, controller=controller) -> None:
                build_group_details(host, group=group, controller=controller)

        return LazyGroupBox(
            title,
            builder=builder,
            parent=self.content_widget,
            animation=HeightSlideAnimation(ANIM_TIME),
            collapsed=True,
            description=schema_tr(section.description),
            object_name=object_name,
        )

    @staticmethod
    def _page_builder(spec: PageSpec):
        """自定义页的构建回调（``build`` 可能是 None）。"""
        build = spec.build
        if build is None:
            return None

        def builder(host: QWidget) -> None:
            build(host)

        return builder

    def _clear_sections(self) -> None:
        for button in self._nav_buttons:
            self.remove_nav_entry(button)
        self._nav_buttons.clear()

        for box in self._section_boxes:
            self.content_layout.removeWidget(box)
            box.setParent(None)
            box.deleteLater()
        self._section_boxes.clear()

    # ------------------------------------------------------------------ #
    # 导航 / 状态
    # ------------------------------------------------------------------ #
    def jump_to(self, box: LazyGroupBox) -> None:
        """展开并滚动到某个章节（展开会顺带建出内容）。"""
        if box is None:
            return
        box.ensure_built()
        box.setChecked(True)
        self.content_layout.activate()
        self.scroll_to_widget_top(box)
        # 布局在动画的第一帧之后才最终安定，再滚一次更准
        QTimer.singleShot(0, lambda target=box: self.scroll_to_widget_top(target))

    def _update_summary(self, schema: dict[str, Any]) -> None:
        summary = schema.get("summary") or {}
        item_count = summary.get("item_count")
        group_count = summary.get("group_count")
        files = list((schema.get("by_file") or {}).keys())

        parts: list[str] = []
        if isinstance(item_count, int):
            parts.append(
                self.tr("{count} settings").format(count=item_count)
            )
        if isinstance(group_count, int):
            parts.append(
                self.tr("{count} groups").format(count=group_count)
            )
        if files:
            parts.append(
                self.tr("writes {count} files").format(count=len(files))
            )
        parts.append(self.tr("same structure as albusctl setting.schema"))
        self.summary_label.setText("  ·  ".join(parts))

        if not schema.get("groups"):
            self.status_label.setText(
                self.tr("No setting tree available (the config system may "
                        "not be ready yet).")
            )

    def _on_changed(self, key: str, result: dict) -> None:
        written = list(result.get("written") or ())
        if written:
            self.status_label.setText(f"\u2714 {key} \u2192 {', '.join(written)}")
        else:
            self.status_label.setText(
                "\u2714 " + self.tr("{key} updated").format(key=key)
            )

    def _on_values_applied(self, values: dict) -> None:
        """值已写入内存后的旁路动作：目前只有「切语言」。

        为什么延迟：写回是在 ``SettingRow._on_commit`` 的调用栈里发生的，
        而切语言会重建整个设置页（行控件被 deleteLater）。等栈退出再切，
        既避免访问已删除的控件，也让这一行的 ✔ 反馈显示完。
        """
        language = values.get("appearance.language")
        if not language:
            return

        def switch() -> None:
            from ..i18n import translation_manager

            translation_manager().install(str(language))

        QTimer.singleShot(0, switch)

    def _on_failed(self, key: str, message: str) -> None:
        self.status_label.setText(f"\u2716 {key}: {message}")

    # ------------------------------------------------------------------ #
    # 语言
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """语言切换：刷新头部文案并重建章节（schema 文案走 ``schema_tr``）。"""
        super().retranslate_ui()
        if not hasattr(self, "title_label"):
            return
        self.title_label.setText(self.tr("Settings"))
        self.reload_button.setToolTip(
            self.tr("Reload the setting tree (setting.schema)")
        )
        self.reload_schema()

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def showEvent(self, event):  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 第一次露面时自动展开第一章，免得整页只剩标题
        if self._auto_expanded or not self._section_boxes:
            return
        self._auto_expanded = True
        first = self._section_boxes[0]
        first.ensure_built()
        first.setChecked(True)


if __name__ == "__main__":  # pragma: no cover - 手工预览用
    import sys

    app = QApplication(sys.argv)
    window = Setting()
    window.resize(820, 620)
    window.setStyleSheet("""
        #SettingSetting {
            background: #222;
        }
        QGroupBox#SettingSection, QGroupBox#SettingPage {
            border: 1px solid #555;
            border-radius: 10px;
            margin-top: 1.2em;
            padding: 6px;
            color: #eee;
            font-weight: bold;
        }
        #SettingRowLabel { color: #eee; }
        #SettingRowDescription { color: #999; font-size: 0.85em; }
        #SettingRowEditor {
            background: #333; color: #eee; border: 1px solid #555;
            border-radius: 6px; padding: 2px 6px; min-height: 24px;
        }
        #SettingJumpButton {
            text-align: left; border: none; background: transparent;
            color: #ddd; padding: 6px;
        }
        """)
    window.show()
    sys.exit(app.exec())
