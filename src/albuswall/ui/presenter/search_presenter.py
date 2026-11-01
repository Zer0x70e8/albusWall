#
"""搜索栏 presenter：按标签过滤当前相册（简单模式）。

模式约定
========

* **简单模式**：输入标签名 → ``TagService.search``（只看当前相册 / 当前
  scope 内至少有一张资产的标签）→ 唯一命中就直接应用，多命中弹候选列表；
  输入清空 = 清除过滤；
* **命令模式**：占位，只提示「尚未实现」。

职责边界：本 presenter 只做「输入 → 标签候选」与「选择 → 过滤意图」，
不直接操作网格、不拼 SQL；资产过滤由内容网格 presenter 执行。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

from PySide6.QtCore import QTimer, Signal

from albuswall.dto.tag.query import TagMatch
from albuswall.dto.utils import normalize_tag_name
from albuswall.service.tag import TagError

from .base import Presenter, register_presenter

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.widgets.floating_search_bar import FloatingSearchBar
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["SearchPresenter"]

logger = logging.getLogger(__package__)

#: 搜索建议的条数上限。
SUGGEST_LIMIT = 20

#: 输入防抖（毫秒）：边打字边查库，但不要每个字符都查。
DEBOUNCE_MS = 220


class SearchPresenter(Presenter):
    """浮动搜索栏 presenter。"""

    name = "search"

    #: 过滤意图：标签 uuid 字符串；``None`` 表示清除过滤。
    filter_requested = Signal(object)
    #: 面向用户的提示（例如「没有匹配的标签」）。
    error = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._view: FloatingSearchBar = window.title_bar.floating_search_bar
        self._tag_service = self.service("tag_service")
        self._view_service = self.service("view_service")

        #: 搜索作用域：由主 presenter 跟随当前相册更新。
        self._album_uuid: Any | None = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_suggestions)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        view = self._view
        view.text_changed.connect(self._on_text_changed)
        view.mode_changed.connect(self._on_mode_changed)
        view.search.connect(self._on_search)
        view.tag_chosen.connect(self._on_tag_chosen)

    def on_stop(self) -> None:
        self._debounce.stop()

    def retranslate(self) -> None:
        """语言切换：建议列表里的标签名来自数据，重建一次即可。"""
        if self._view.line_edit.text().strip():
            self._debounce.start()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_album(self, album: Any | None) -> None:
        """切换搜索作用域（切相册时清掉回显，不广播过滤意图）。"""
        self._album_uuid = None if album is None else album.uuid
        self.reset_view()

    def apply_filter(self, tag_uuid: Any) -> None:
        """外部（例如点了标签 chip）直接应用某标签过滤，并同步搜索框。"""
        uuid_str = str(tag_uuid)
        self._set_text(self._label_of(uuid_str))
        self._view.hide_suggestions()
        self.filter_requested.emit(uuid_str)

    def clear_filter(self) -> None:
        """清除过滤（搜索框回显一并清空）。"""
        self._set_text("")
        self._view.hide_suggestions()
        self.filter_requested.emit(None)

    def reset_view(self) -> None:
        """只清空回显，不广播过滤意图。"""
        self._debounce.stop()
        self._set_text("")
        self._view.hide_suggestions()

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_text_changed(self, text: str) -> None:
        if self._is_command_mode():
            self._view.hide_suggestions()
            return
        if not str(text).strip():
            self._debounce.stop()
            self._view.hide_suggestions()
            # 清空输入 = 立刻取消过滤，避免关掉搜索栏后留下隐形过滤
            self.filter_requested.emit(None)
            return
        self._debounce.start()

    def _on_mode_changed(self, _mode: str) -> None:
        self._debounce.stop()
        if self._is_command_mode():
            self._view.hide_suggestions()
            return
        if self._view.line_edit.text().strip():
            self._debounce.start()

    def _on_search(self, payload: dict) -> None:
        self._debounce.stop()
        self._view.hide_suggestions()

        if payload.get("option") == self._view.MODE_COMMAND:
            self.error.emit(self.tr("命令模式尚未实现"))
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            self.filter_requested.emit(None)
            return

        matches = self._search(text)
        if not matches:
            self.error.emit(
                self.tr("当前相册没有匹配的标签：{text}").format(text=text))
            return

        exact = self._exact_match(text, matches)
        if exact is not None:
            self._apply(str(exact.uuid), text)
            return
        if len(matches) == 1:
            self._apply(str(matches[0].uuid), text)
            return
        self._show_suggestions(matches)

    def _on_tag_chosen(self, tag_uuid: str) -> None:
        self._apply(tag_uuid, self._view.line_edit.text().strip())

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def _scope(self) -> str:
        """当前相册的 scope（``active`` / ``deleted``）。"""
        if self._album_uuid is None or self._view_service is None:
            return "active"
        try:
            scope = self._view_service.get_scope(self._album_uuid)
        except Exception:  # noqa: BLE001
            logger.exception("search presenter: get_scope failed")
            return "active"
        return scope or "active"

    def _search(self, keyword: str) -> list[TagMatch]:
        if self._tag_service is None or self._album_uuid is None:
            return []
        scope = self._scope()
        try:
            if scope == "active":
                return self._tag_service.search(
                    keyword, album_uuid=self._album_uuid,
                    limit=SUGGEST_LIMIT,
                )
            return self._tag_service.search(
                keyword, scope=scope, limit=SUGGEST_LIMIT)
        except Exception:  # noqa: BLE001 - 搜索失败按无结果处理
            logger.exception("search presenter: tag search failed: %r", keyword)
            return []

    def _refresh_suggestions(self) -> None:
        if self._is_command_mode():
            return
        text = self._view.line_edit.text().strip()
        if not text:
            self._view.hide_suggestions()
            return
        matches = self._search(text)
        if not matches:
            self._view.hide_suggestions()
            return
        self._show_suggestions(matches)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _apply(self, tag_uuid: str, text: str) -> None:
        label = self._label_of(tag_uuid) or text
        self._set_text(label)
        self._view.hide_suggestions()
        self.filter_requested.emit(tag_uuid)

    def _show_suggestions(self, matches: Sequence[TagMatch]) -> None:
        self._view.set_suggestions([
            (str(match.uuid), f"{match.label}  ({match.asset_count})")
            for match in matches
        ])

    def _label_of(self, tag_uuid: str) -> str:
        if self._tag_service is None:
            return ""
        try:
            return self._tag_service.path_label(tag_uuid)
        except TagError:
            return ""
        except Exception:  # noqa: BLE001
            logger.exception("search presenter: path_label failed")
            return ""

    def _set_text(self, text: str) -> None:
        """写回输入框但不触发「文本变化 → 重新搜索」的回环。"""
        blocked = self._view.line_edit.blockSignals(True)
        try:
            self._view.line_edit.setText(text)
        finally:
            self._view.line_edit.blockSignals(blocked)

        combo = self._view.option_combo
        combo_blocked = combo.blockSignals(True)
        try:
            index = combo.findData(self._view.MODE_SIMPLE)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(combo_blocked)

    def _is_command_mode(self) -> bool:
        return self._view.mode == self._view.MODE_COMMAND

    @staticmethod
    def _exact_match(
            text: str,
            matches: Sequence[TagMatch],
    ) -> TagMatch | None:
        target = normalize_tag_name(text)
        for match in matches:
            if normalize_tag_name(match.tag.name) == target:
                return match
            if normalize_tag_name(match.label) == target:
                return match
        return None


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_search_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("search presenter skipped: no window bound")
        return None
    return SearchPresenter(container, window=window, parent=main)
