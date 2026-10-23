#
"""搜索栏 presenter —— 简单模式：按标签过滤当前相册。

模式约定
--------
* ``简单模式``：输入标签名，搜索当前相册（或当前虚拟相册 scope）内
  至少挂有一张资产的标签；选中 / 回车后把过滤条件交给 manager，
  由 manager 把「相册资产集合」与「标签资产集合」求交后刷新网格。
  输入为空时提交搜索 = 清除过滤。
* ``命令模式``：占位选项，暂不实现，只提示未实现。

职责边界：本 presenter 只做「输入 → 标签候选」与「选择 → 过滤意图」，
不直接操作网格、不碰 SQL 拼接；资产过滤由 manager 统一执行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Sequence
from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal

from albuswall.dto.utils import normalize_tag_name
from albuswall.log import getLogger
from albuswall.services.tag import TagError, TagMatch, TagService
from albuswall.ui.builtin.utils.signal_adapter import SignalAdapter

if TYPE_CHECKING:
    from albuswall.dto.album import Album
    from albuswall.services.view import ViewService
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
    )

__all__ = ["SearchPresenter"]

_logger = getLogger(__name__)

_SUGGEST_LIMIT = 20
_DEBOUNCE_MS = 220


class SearchPresenter(QObject):
    #: 过滤意图：标签 uuid 字符串；``None`` 表示清除过滤。
    filter_requested = Signal(object)
    error_occurred = Signal(str)

    def __init__(
            self,
            view: "FloatingSearchBar",
            tag_service: TagService,
            view_service: Optional["ViewService"],
            current_album_provider: Callable[[], Optional["Album"]],
            parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._view = view
        self._tag = tag_service
        self._view_service = view_service
        self._current_album = current_album_provider

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_suggestions)

        self._adapter = SignalAdapter(self)
        self._adapter.connect(view.text_changed, self._on_text_changed)
        self._adapter.connect(view.mode_changed, self._on_mode_changed)
        self._adapter.connect(view.search, self._on_search)
        self._adapter.connect(view.tag_chosen, self._on_tag_chosen)

    # interface
    def apply_filter(self, tag_uuid: UUID | str) -> None:
        """外部（如 tag chip 点击）直接应用某标签过滤，并同步搜索框。"""
        uuid_str = str(tag_uuid)
        try:
            label = self._tag.path_label(uuid_str)
        except TagError:
            label = ""
        except Exception:
            _logger.exception("resolve tag label failed: %s", uuid_str)
            label = ""
        self._set_text(label)
        self._view.hide_suggestions()
        self.filter_requested.emit(uuid_str)

    def clear_filter(self) -> None:
        self._set_text("")
        self._view.hide_suggestions()
        self.filter_requested.emit(None)

    def reset_view(self) -> None:
        """只清空搜索框回显，不广播过滤意图（供 manager 切换相册时用）。"""
        self._debounce.stop()
        self._set_text("")
        self._view.hide_suggestions()

    # slot
    def _on_text_changed(self, text: str) -> None:
        if self._is_command_mode():
            self._view.hide_suggestions()
            return
        if not text.strip():
            self._debounce.stop()
            self._view.hide_suggestions()
            # 清空输入 = 立即取消过滤，避免关掉搜索栏后留下隐形过滤
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
            self.error_occurred.emit(self.tr("命令模式尚未实现"))
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            # 空搜索 = 清除过滤
            self.filter_requested.emit(None)
            return

        matches = self._search(text)
        if not matches:
            self.error_occurred.emit(
                self.tr("当前相册没有匹配的标签：{text}").format(text=text)
            )
            return

        exact = self._exact_match(text, matches)
        if exact is not None:
            self._apply(str(exact.uuid), text)
            return
        if len(matches) == 1:
            self._apply(str(matches[0].uuid), text)
            return
        self._show_suggestions(matches)

    def _on_tag_chosen(self, uuid: str) -> None:
        self._apply(uuid, self._view.line_edit.text().strip())

    # search
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

    def _search(self, keyword: str) -> list[TagMatch]:
        album = self._current_album()
        if album is None:
            return []

        album_uuid = getattr(album, "uuid", None)
        if album_uuid is None:
            return []

        scope: Optional[str] = None
        if self._view_service is not None:
            try:
                scope = self._view_service.get_scope(album_uuid)
            except Exception:
                _logger.exception("resolve album scope failed: %s", album_uuid)

        try:
            if scope is None:
                return self._tag.search(
                    keyword,
                    album_uuid=album_uuid,
                    limit=_SUGGEST_LIMIT,
                )
            return self._tag.search(
                keyword, scope=scope, limit=_SUGGEST_LIMIT,
            )
        except Exception:
            _logger.exception("tag search failed: %r", keyword)
            return []

    # builtin
    def _apply(self, tag_uuid: str, text: str) -> None:
        try:
            label = self._tag.path_label(tag_uuid)
        except Exception:
            label = text
        self._set_text(label)
        self._view.hide_suggestions()
        self.filter_requested.emit(tag_uuid)

    def _show_suggestions(self, matches: Sequence[TagMatch]) -> None:
        self._view.set_suggestions([
            (str(m.uuid), f"{m.label}  ({m.asset_count})")
            for m in matches
        ])

    def _set_text(self, text: str) -> None:
        """写回输入框但不触发「文本变化 → 重新搜索」的回环。"""
        blocked = self._view.line_edit.blockSignals(True)
        self._view.line_edit.setText(text)
        self._view.line_edit.blockSignals(blocked)

        # 过滤默认走简单模式；切模式本身也会触发信号，这里一并静默。
        # 用稳定标识（MODE_SIMPLE）而不是下拉框文本，语言切换不会影响判断。
        combo = self._view.option_combo
        combo_blocked = combo.blockSignals(True)
        index = combo.findData(self._view.MODE_SIMPLE)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(combo_blocked)

    def _is_command_mode(self) -> bool:
        return self._view.mode == self._view.MODE_COMMAND

    @staticmethod
    def _exact_match(
            text: str,
            matches: Sequence[TagMatch],
    ) -> Optional[TagMatch]:
        target = normalize_tag_name(text)
        for match in matches:
            if normalize_tag_name(match.tag.name) == target:
                return match
            if normalize_tag_name(match.label) == target:
                return match
        return None

    # life cycle
    def setup(self, _=None) -> None:
        pass

    def teardown(self) -> None:
        self._debounce.stop()
        self._adapter.disconnect_all()
