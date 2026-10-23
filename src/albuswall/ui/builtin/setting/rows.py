#
"""单个设置项的一行：标签 + 编辑器 + 状态。

一行只关心三件事：

1. 把 item dict 里的 label / description / path 显示出来；
2. 用户改完之后**立刻**写回（失败就地显示错误，不弹窗）；
3. 提供一个「恢复默认」的小按钮。

写回由 :class:`~albuswall.ui.builtin.setting.controller.SettingController`
负责，本类不碰配置对象。

同一个文件里还有 :class:`ExtraRow`：把 ``extras.py`` 注册的「非配置行」
（动作按钮 / 纯说明 / 自定义控件）画成和设置项一模一样的一行，这样界面
只认「一行」这个概念，不需要区分它背后有没有配置节点。

所有面向用户的文字都是英文；能拿到 QObject 上下文的地方用 ``self.tr()``，
否则用 :func:`_tr`（同一个翻译上下文，便于以后接 ``QTranslator``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from .controller import SettingController
from .editors import Editor, create_editor
from .translation import schema_tr

__all__ = ["ExtraRow", "SettingRow"]

_STATUS_HOLD_MS = 4000

#: 翻译上下文（与 QSS 里的 objectName 无关，只用于 i18n）
_TR_CONTEXT = "SettingRow"


def _tr(text: str) -> str:
    """模块级翻译入口（没有 QObject 时的 ``tr``）。"""
    return QCoreApplication.translate(_TR_CONTEXT, text)


class SettingRow(QWidget):
    """一个可编辑设置项。"""

    def __init__(
            self,
            item: dict[str, Any],
            controller: SettingController,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingRow")

        self._item = item
        self._controller = controller
        self._editor: Editor = create_editor(item)

        # 状态文字（✔/✖）显示一会儿就自动清掉，别一直占着位置
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._clear_status)

        self._build_ui()
        self._apply_readonly()
        self._controller.register_row(self)

    # ------------------------------------------------------------------ #
    # 结构
    # ------------------------------------------------------------------ #
    @property
    def key(self) -> str:
        """本行对应的配置项 key（如 ``appearance.theme``）。"""
        return str(self._item.get("key") or "")

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        item = self._item

        self.label = QLabel(
            schema_tr(item.get("label") or item.get("name") or "?")
        )
        self.label.setObjectName("SettingRowLabel")
        self.label.setToolTip(item.get("path") or "")

        self.status = QLabel("")
        self.status.setObjectName("SettingRowStatus")
        self.status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self.reset_button = QToolButton()
        self.reset_button.setObjectName("SettingRowResetButton")
        self.reset_button.setText("\u21ba")  # ↺
        self.reset_button.setAutoRaise(True)
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setToolTip(
            f"{self.tr('Reset to default')}: {_describe_default(item)}"
        )
        self.reset_button.clicked.connect(self._on_reset)

        self.description = QLabel(_describe_item(item))
        self.description.setObjectName("SettingRowDescription")
        self.description.setWordWrap(True)

        self.error = QLabel("")
        self.error.setObjectName("SettingRowError")
        self.error.setWordWrap(True)
        self.error.hide()

        label_box = QWidget(self)
        label_layout = QHBoxLayout(label_box)
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_layout.setSpacing(6)
        label_layout.addWidget(self.label)
        hint = (self._item.get("hint") or "").strip()
        if hint:
            self.hint = QLabel(f"({schema_tr(hint)})", label_box)
            self.hint.setObjectName("SettingRowHint")
            label_layout.addWidget(self.hint)
        label_layout.addStretch()

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 3, 0, 3)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(0, 1)
        grid.addWidget(label_box, 0, 0)
        grid.addWidget(self._editor.widget, 0, 1)
        grid.addWidget(self.reset_button, 0, 2)
        grid.addWidget(self.status, 0, 3)
        grid.addWidget(self.description, 1, 0, 1, 4)
        grid.addWidget(self.error, 2, 0, 1, 4)

        # 编辑器拉得太宽会很难看：给它一个合理的伸缩策略
        self._editor.widget.setMinimumWidth(180)
        self._editor.widget.setMaximumWidth(420)

        self._editor.connect_commit(self._on_commit)

    def _apply_readonly(self) -> None:
        item = self._item
        read_only = bool(item.get("readonly")) or not bool(
            item.get("editable", True)
        )
        if read_only:
            self._editor.set_read_only(True)
            self.reset_button.hide()
            reason = self.tr("the value comes from a read-only source") \
                if item.get("readonly") else self.tr("this item is not editable")
            self.label.setToolTip(f"{item.get('path') or ''}\n{reason}")
        if item.get("read_error"):
            self._show_error(str(item["read_error"]))

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def _on_commit(self) -> None:
        """用户改完一个值：交给控制器写回。

        如果这一项带 ``patch.apply`` 写回钩子，就用钩子把「一个值」翻译成
        「多个配置节点」（例如选主题要写 ``[ui]theme`` + ``[path]theme``）。
        """
        try:
            value = self._editor.value()
        except Exception as exc:  # noqa: BLE001  输入本身非法
            self._show_error(str(exc))
            return

        key = self.key
        hook = getattr(self._item.get("patch"), "apply", None)
        if callable(hook):
            try:
                proposal = hook(value, dict(self._item))
            except Exception as exc:  # noqa: BLE001  钩子自己失败了
                self._show_error(str(exc))
                return
            values = _merge_proposal(proposal, key, value)
        else:
            values = {key: value}

        # 只写真正变了的节点：反复选同一个主题不会白写一遍盘
        pending = {
            name: new for name, new in values.items()
            if self._is_change(name, new)
        }
        if not pending:
            self._clear_status()
            return

        try:
            result = self._controller.apply_many(pending, edited=key)
        except Exception as exc:  # noqa: BLE001  写盘 / 校验失败
            self._show_error(str(exc))
            return

        self._show_ok(result)

    def _is_change(self, name: str, value: Any) -> bool:
        """``value`` 和该节点的当前值不同吗？"""
        from .controller import MISSING

        current = self._controller.current_value(name)
        if current is MISSING:
            # 结构里没有这个节点（比如补丁写了别的 key）：交给后端判断
            return True
        return not _unchanged(value, current)

    # ------------------------------------------------------------------ #
    # 被连带修改后就地刷新（不重建控件、不动滚动位置）
    # ------------------------------------------------------------------ #
    def refresh_from_schema(self, schema: dict[str, Any]) -> None:
        """按最新结构更新这一行的显示值 / 说明。"""
        for item in schema.get("items") or []:
            if not isinstance(item, dict) or item.get("key") != self.key:
                continue
            self._item = item
            self._editor.set_value(item.get("value"))
            self.label.setText(
                schema_tr(item.get("label") or self.key)
            )
            self.description.setText(_describe_item(item))
            self.reset_button.setToolTip(
                f"{self.tr('Reset to default')}: {_describe_default(item)}"
            )
            return

    def _on_reset(self) -> None:
        """把编辑器恢复成登记表里的默认值并立即写回。"""
        self._editor.set_value(self._item.get("default"))
        self._on_commit()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _show_ok(self, result: Any) -> None:
        self.error.hide()
        written = []
        if isinstance(result, dict):
            written = list(result.get("written") or ())
        self.status.setText("\u2714")
        if written:
            header = self.tr("Written to:")
            self.status.setToolTip(f"{header}\n" + "\n".join(written))
        else:
            self.status.setToolTip(self.tr("Written to the config"))
        self._hold_status()

    def _show_error(self, message: str) -> None:
        self.status.setText("\u2716")
        self.status.setToolTip(message)
        self.error.setText(message)
        self.error.show()
        self._hold_status()

    def _hold_status(self) -> None:
        self._status_timer.start(_STATUS_HOLD_MS)

    def _clear_status(self) -> None:
        self.status.setText("")
        self.status.setToolTip("")
        self.error.hide()
        self.error.setText("")


class ExtraRow(QWidget):
    """分组里的一条「非配置行」：注册项 → 一行控件。

    内容来自 :class:`~albuswall.ui.builtin.setting.extras.ExtraSpec`：

    * 标题（``title``）和说明（``description``）永远显示；
    * ``build`` 给出的控件放在行尾；
    * ``button`` 非空时再加一个按钮，``clicked`` 为空就是**占位按钮**
      （点了什么也不做，用于「以后再说」的动作）。

    因为只认属性不认类型，这一行也可以由插件自带的 spec 对象驱动。
    """

    def __init__(self, spec: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingExtraRow")
        self._spec = spec

        title = getattr(spec, "title", "") or getattr(spec, "id", "?")
        description = (getattr(spec, "description", "") or "").strip()

        self.label = QLabel(schema_tr(title))
        self.label.setObjectName("SettingRowLabel")

        self.description = QLabel(schema_tr(description))
        self.description.setObjectName("SettingRowDescription")
        self.description.setWordWrap(True)
        self.description.setVisible(bool(description))

        self.custom = self._build_custom(spec)
        self.button = self._build_button(spec)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 3, 0, 3)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(0, 1)
        grid.addWidget(self.label, 0, 0)

        column = 1
        if self.custom is not None:
            self.custom.setMinimumWidth(180)
            self.custom.setMaximumWidth(420)
            grid.addWidget(self.custom, 0, column)
            column += 1
        if self.button is not None:
            grid.addWidget(self.button, 0, column)
            column += 1
        grid.addWidget(self.description, 1, 0, 1, max(column, 1))

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build_custom(self, spec: Any) -> QWidget | None:
        """``build`` 返回的自定义控件（失败只记日志，不影响整行）。"""
        build = getattr(spec, "build", None)
        if not callable(build):
            return None
        try:
            widget = build(self)
        except Exception:  # noqa: BLE001
            from albuswall.log import getLogger

            getLogger(__name__).exception(
                "custom setting row %r failed to build",
                getattr(spec, "id", "?"),
            )
            return None
        return widget if isinstance(widget, QWidget) else None

    def _build_button(self, spec: Any) -> QPushButton | None:
        """按钮行；``clicked`` 为空时按钮照常显示但什么也不做。"""
        text = str(getattr(spec, "button", "") or "")
        clicked = getattr(spec, "clicked", None)
        if not text and not callable(clicked):
            return None

        # 动作按钮文案来自插件注册的 spec（见 extras.py），与 schema 文案
        # 同源，走同一个翻译上下文
        button = QPushButton(schema_tr(text) or self.tr("Edit"), self)
        button.setObjectName("SettingRowActionButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if callable(clicked):
            button.clicked.connect(lambda _checked=False: clicked())
        else:
            # 空按钮：明确告诉用户「还没接线」，而不是假装点了没反应
            button.setToolTip(self.tr("Reserved: this action is not wired yet."))
        return button


# --------------------------------------------------------------------------- #
# 文本辅助
# --------------------------------------------------------------------------- #
def _merge_proposal(
        proposal: Any,
        key: str,
        value: Any,
) -> dict[str, Any]:
    """把写回钩子的返回值收敛成 ``{key: value}``。

    钩子返回 ``None`` / 空 dict 表示「我自己不接管写回」，那就退回「写本项」。
    """
    if not proposal:
        return {key: value}
    if not isinstance(proposal, Mapping):
        raise ValueError(
            _tr(
                "Write hook of setting item {key} must return "
                "{{config key: value}}, got {type}"
            ).format(key=key, type=type(proposal).__name__)
        )
    merged = {str(name): new for name, new in proposal.items()}
    merged.setdefault(key, value)
    return merged


def _describe_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    description = (item.get("description") or "").strip()
    if description:
        parts.append(schema_tr(description))
    value = item.get("value")
    if isinstance(value, str) and "${" in value:
        # 值里还有 ${...} 插值（比如样式表默认值）→ 说明它引用的项还没定
        parts.append(
            _tr("contains a ${…} placeholder; resolved once the related "
                "items are set")
        )

    path = item.get("path")
    if path:
        parts.append(str(path))
    target = item.get("target_file")
    if target:
        parts.append(f"\u2192 {target}")
    if item.get("dirty"):
        parts.append(_tr("not written yet"))
    return "   ".join(parts)


def _describe_default(item: dict[str, Any]) -> str:
    default = item.get("default")
    if default is None:
        return _tr("(none)")
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


def _unchanged(new: Any, old: Any) -> bool:
    """新值是否和当前值等价（避免「点一下没改也写盘」）。"""
    if new is old:
        return True
    if isinstance(new, (int, float)) and isinstance(old, (int, float)) \
            and not isinstance(new, bool) and not isinstance(old, bool):
        return float(new) == float(old)
    try:
        return bool(new == old)
    except Exception:  # noqa: BLE001  奇怪的比较结果不算相同
        return False
