#
"""导入源页 presenter：卡片列表 + 详情表单 + 右键菜单。

职责
====

* 持有卡片模型（``SourceCardModel``）并按 uuid 对齐刷新；
* 把视图意图翻译成 ``SourceService`` 写入（新建 / PATCH / 启用禁用 / 删除）；
* 表单 ⇄ DTO 的桥在 :mod:`.source_form`（视图不认 DTO，DTO 不认 Qt）；
* 后端线程回调（``sources_changed`` / ``scan_finished``）只通过 Qt 信号的
  ``QueuedConnection`` 搬回主线程，槽里才碰 UI。

数据身份
========

卡片行号 ⇄ 源 id 的映射由 ``SourceCardModel.uuid`` 保存（``str(id)``）：
本 presenter 只维护一份 ``row → id`` 的旁路表用于菜单操作，视图侧永远
按行号说话。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from PySide6.QtCore import QModelIndex, QPoint, Qt, Signal
from PySide6.QtWidgets import QMenu

from albuswall.dto.source.command import form_from_source
from albuswall.dto.source.query import MANUAL_SOURCE_ID
from albuswall.ui.model import SourceCardModel

from .base import Presenter, register_presenter
from .source_form import apply_form, close_form, form_to_create, form_to_update, read_form

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.source.core import IngestSource as IngestSourceView
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["SourcePresenter"]

logger = logging.getLogger(__package__)

#: 新建草稿行的哨兵 id（不落库；提交后才变成真实 id）。
DRAFT_SOURCE_ID: Final[int] = -1


class SourcePresenter(Presenter):
    """导入源页 presenter。"""

    name = "source"

    #: 源列表发生变化（成功写入后）。
    sources_changed = Signal()
    #: 面向用户的错误（窗口 toast）。
    error = Signal(str)
    #: 后端线程 → 主线程的刷新桥（``QueuedConnection``，槽里才碰 UI）。
    backend_refresh = Signal()

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._view: IngestSourceView = window.source
        self._source_service = self.service("source_service")
        self._trash_service = self.service("source_trash_service")

        self._model = SourceCardModel(parent=self)
        self._row_ids: list[int] = []
        self._current_id: int | None = None
        self._draft_dto: Any | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        view = self._view
        view.set_model(self._model)
        view.card_clicked.connect(self._on_card_clicked)
        view.context_menu_requested.connect(self._on_context_menu_requested)
        view.add_button.clicked.connect(self._on_add_requested)
        view.apply_button.clicked.connect(self._on_apply_requested)
        view.detail_closed.connect(self._on_detail_closed)

        self.backend_refresh.connect(
            self.reload, Qt.ConnectionType.QueuedConnection)

        self._bind_backend()
        self.reload()

    def on_stop(self) -> None:
        self._unbind_backend()

    def retranslate(self) -> None:
        """语言切换：卡片标题里的默认兜底文案要重新取译文。"""
        self.reload()

    # ------------------------------------------------------------------ #
    # 后端信号（工作线程 → 主线程）
    # ------------------------------------------------------------------ #
    def _bind_backend(self) -> None:
        if self._trash_service is not None:
            try:
                self._trash_service.sources_changed.connect(
                    self._on_sources_changed)
            except (AttributeError, RuntimeError, TypeError):
                logger.warning("source presenter: sources_changed unavailable")
        runtime = self.service("source_runtime")
        if runtime is not None:
            try:
                runtime.scan_finished.connect(self._on_scan_finished)
            except (AttributeError, RuntimeError, TypeError):
                logger.debug("source presenter: scan_finished unavailable")

    def _unbind_backend(self) -> None:
        if self._trash_service is not None:
            try:
                self._trash_service.sources_changed.disconnect(
                    self._on_sources_changed)
            except (AttributeError, RuntimeError, TypeError):
                pass
        runtime = self.service("source_runtime")
        if runtime is not None:
            try:
                runtime.scan_finished.disconnect(self._on_scan_finished)
            except (AttributeError, RuntimeError, TypeError):
                pass

    def _on_sources_changed(self, _sender: object, **_kwargs: Any) -> None:
        """后端线程回调：只把刷新请求搬回主线程，绝不碰 UI。"""
        self.backend_refresh.emit()

    def _on_scan_finished(self, _sender: object, **kwargs: Any) -> None:
        """扫描完成（观测用，进度提示在将来的状态栏里加）。"""
        event = kwargs.get("event")
        logger.debug(
            "source presenter: scan finished source_id=%s",
            getattr(event, "source_id", None))

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    @property
    def model(self) -> SourceCardModel:
        return self._model

    def _default_title(self) -> str:
        return self.tr("Untitled ingest source")

    def reload(self) -> None:
        """从数据库全量拉取导入源，刷新卡片列表。"""
        ids: list[int] = []
        cards: list[Any] = []

        if self._draft_dto is not None:
            ids.append(DRAFT_SOURCE_ID)
            cards.append(self._draft_dto)

        dtos: list[Any] = []
        if self._source_service is not None:
            try:
                dtos = list(self._source_service.list_sources() or ())
            except Exception:  # noqa: BLE001
                logger.exception("source presenter: list_sources failed")
                dtos = []
        for dto in dtos:
            ids.append(int(dto.id))
            cards.append(dto)

        self._row_ids = ids
        self._model.set_cards(cards)

        if self._current_id is not None and self._current_id not in ids:
            self._current_id = None
            close_form(self._view.detail_widget)

    def _dto_for_id(self, source_id: int) -> Any | None:
        if source_id == DRAFT_SOURCE_ID:
            return self._draft_dto
        if self._source_service is None:
            return None
        try:
            return self._source_service.get_source(source_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "source presenter: get_source failed (id=%s)", source_id)
            return None

    # ------------------------------------------------------------------ #
    # 视图交互
    # ------------------------------------------------------------------ #
    def _source_id_at(self, index: QModelIndex) -> int | None:
        if not index.isValid():
            return None
        row = int(index.row())
        if not 0 <= row < len(self._row_ids):
            return None
        return self._row_ids[row]

    def _on_card_clicked(self, index: QModelIndex) -> None:
        source_id = self._source_id_at(index)
        if source_id is None:
            return
        self._current_id = source_id
        dto = self._dto_for_id(source_id)
        if dto is None:
            self.reload()
            return
        apply_form(self._view.detail_widget, form_from_source(dto))

    def _on_detail_closed(self) -> None:
        """关闭详情：未提交的草稿直接丢弃。"""
        if self._draft_dto is not None:
            self._draft_dto = None
            self._current_id = None
            self.reload()

    def _on_add_requested(self) -> None:
        """顶部「添加」：造一张内存草稿并直接打开详情页。"""
        if self._source_service is None:
            self.error.emit(self.tr("The ingest source service is unavailable"))
            return
        if self._draft_dto is None:
            self._draft_dto = _make_draft(self._default_title())
            self.reload()
        self._open_row(0)

    def _on_apply_requested(self) -> None:
        """详情页「应用」：草稿态新建，普通态 PATCH。"""
        if self._current_id is None:
            return
        if self._current_id == DRAFT_SOURCE_ID:
            self._create_from_draft()
            return
        if self._source_service is None:
            return
        patch = form_to_update(read_form(self._view.detail_widget))
        try:
            self._source_service.update(self._current_id, patch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("source presenter: update failed")
            self.error.emit(str(exc))
            return
        self.reload()

    def _create_from_draft(self) -> None:
        if self._source_service is None:
            return
        form = read_form(self._view.detail_widget)
        payload = form_to_create(form, self._default_title())
        try:
            new_id = self._source_service.create(payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("source presenter: create failed")
            self.error.emit(str(exc))
            return
        self._draft_dto = None
        self.reload()
        self._current_id = new_id
        self._open_source(new_id)
        self.sources_changed.emit()

    def _open_row(self, row: int) -> None:
        """让视图打开第 ``row`` 张卡片（触发/跳过详情模式都交给视图）。"""
        index = self._model.index(row, 0)
        if index.isValid():
            self._view.open_card(index)

    def _open_source(self, source_id: int) -> None:
        try:
            row = self._row_ids.index(source_id)
        except ValueError:
            return
        self._open_row(row)

    # ------------------------------------------------------------------ #
    # 右键菜单：删除 / 禁用 / 恢复
    # ------------------------------------------------------------------ #
    def _on_context_menu_requested(
            self, index: QModelIndex, global_pos: QPoint) -> None:
        source_id = self._source_id_at(index)
        if source_id is None or source_id in (DRAFT_SOURCE_ID, MANUAL_SOURCE_ID):
            return
        dto = self._dto_for_id(source_id)
        if dto is None:
            return
        self._exec_context_menu(source_id, dto, global_pos)

    def _exec_context_menu(
            self, source_id: int, dto: Any, global_pos: QPoint) -> None:
        """两态循环：普通态 [删除][禁用/启用]，确认态 [恢复][确认删除]。"""
        disabled = bool(getattr(dto, "disabled", False))
        toggle_label = self.tr("Enable") if disabled else self.tr("Disable")

        confirm_mode = False
        while True:
            menu = QMenu(self._view)
            if confirm_mode:
                restore_action = menu.addAction(self.tr("Restore"))
                confirm_action = menu.addAction(self.tr("Confirm delete"))
                choose = menu.exec(global_pos)
                if choose is None:
                    return
                if choose is restore_action:
                    confirm_mode = False
                    continue
                if choose is confirm_action:
                    self.delete_source(source_id)
                    return
                return

            delete_action = menu.addAction(self.tr("Delete"))
            toggle_action = menu.addAction(toggle_label)
            choose = menu.exec(global_pos)
            if choose is None:
                return
            if choose is delete_action:
                confirm_mode = True
                continue
            if choose is toggle_action:
                self.set_source_disabled(source_id, not disabled)
                return
            return

    # ------------------------------------------------------------------ #
    # 写操作
    # ------------------------------------------------------------------ #
    def delete_source(self, source_id: int) -> bool:
        """删除导入源（回收站可用时走软删 + 异步 purge）。"""
        if source_id == MANUAL_SOURCE_ID:
            self.error.emit(self.tr("A virtual root ingest source cannot be deleted"))
            return False

        service: Any = self._trash_service or self._source_service
        if service is None:
            return False
        try:
            if self._trash_service is not None:
                done = self._trash_service.delete_source(source_id)
            else:
                self._source_service.delete(source_id)
                done = True
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "source presenter: delete failed (id=%s)", source_id)
            self.error.emit(str(exc))
            return False

        if done:
            if source_id == self._current_id:
                self._current_id = None
                close_form(self._view.detail_widget)
            self.reload()
            self.sources_changed.emit()
        return bool(done)

    def set_source_disabled(self, source_id: int, disabled: bool) -> bool:
        """启用 / 禁用导入源（DB 行保留，禁用 ≠ 删除）。"""
        if self._source_service is None:
            return False
        try:
            if disabled:
                self._source_service.disable(source_id)
            else:
                self._source_service.enable(source_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "source presenter: toggle failed (id=%s)", source_id)
            self.error.emit(str(exc))
            return False
        self.reload()
        return True


def _make_draft(title: str) -> Any:
    """构造一张「还没落库」的导入源草稿（与读模型同形状）。"""
    from albuswall.dto.source.query import IngestSource

    return IngestSource(id=DRAFT_SOURCE_ID, title=title, source_path="")


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_source_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = main.window
    if window is None:
        logger.warning("source presenter skipped: no window bound")
        return None
    return SourcePresenter(container, window=window, parent=main)
