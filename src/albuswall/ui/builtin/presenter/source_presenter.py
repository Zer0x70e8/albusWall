#
"""IngestSource 列表 / 详情 presenter。

职责：
  * 创建并持有 model / delegate，把它们装配到 view 上；
  * 把 view 的信号翻译成业务动作（增删改查 → SourceService）；
  * 作为唯一的数据入口 —— 视图层对 repository / DB 完全无感知。

生命周期
--------
manager 通过一对钩子管理本类：

* ``setup(container)``   —— 注入 container 提供的依赖（这里只补服务）；
* ``teardown()``         —— 断开信号、清空会话状态。

两者都必须幂等：manager 可能重复调用，或在构造后立刻调用。
构造期已完成"能做的绑定"，``setup`` 只做 container 里才拿得到的补充。

线程
----
后端 ``SourceExecutor`` / ``SourceTrashService`` 在工作线程里回调本类，
所以回调只做一件事：把事件 emit 成一个 Qt 信号搬回主线程，绝不碰 UI。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Optional

from PySide6.QtCore import QModelIndex, QObject
from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import QMenu

from albuswall.common.enums import FileTypeCheckMode
from albuswall.dto.source import (
    MANUAL_SOURCE_ID,
    IngestSource,
    IngestSourceCreate,
    IngestSourceFormData,
    IngestSourceUpdate,
)
from albuswall.log import getLogger
from albuswall.ui.builtin.delegate.source_card import CardItemDelegate
from albuswall.ui.builtin.model.source_card import CardItem, CardListModel

if TYPE_CHECKING:
    from albuswall.services import SourceService, SourceTrashService

    # 读模型 IngestSource 与窗口类同名，这里重命名避免冲突
    from ..window.source.core import IngestSource as IngestSourceView

__all__ = ["DRAFT_SOURCE_ID", "IngestSourcePresenter"]

_logger = getLogger(__name__)

DRAFT_SOURCE_ID: Final[int] = -1


# noinspection broad-exception
class IngestSourcePresenter(QObject):
    """IngestSource 的 presenter。"""

    # 线程桥：后端在工作线程 emit，我们转成 Qt 信号让 UI 槽在主线程执行。
    _scan_finished_queued = QtSignal(object)
    _refresh_queued = QtSignal()  # 无载荷刷新桥

    def __init__(
            self,
            view: "IngestSourceView",
            source_service: Optional["SourceService"] = None,
            trash_service: Optional["SourceTrashService"] = None,
            parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)

        self._view = view

        # ---- 业务服务（唯一数据入口）----
        self._service: Optional["SourceService"] = source_service
        self._trash: Optional["SourceTrashService"] = trash_service

        # 两条后端信号各自是否已连上（分开记，缺一个不影响另一个）
        self._scan_bound: bool = False
        self._trash_bound: bool = False

        self._torn_down: bool = False

        # ---- 会话状态 ----
        self._draft_dto: Optional[IngestSource] = None
        self._row_source_ids: list[int] = []
        self._current_source_id: Optional[int] = None

        # ---- 数据层 ----
        self._model = CardListModel(parent=self)
        self._delegate = CardItemDelegate(self._view.view)
        self._view.set_model(self._model, self._delegate)

        # ---- 跨线程桥接（先接信号，再挂后端信号）----
        self._scan_finished_queued.connect(self._on_scan_finished_on_ui)
        self._refresh_queued.connect(self._on_refresh_on_ui)

        # ---- 连线 ----
        self._wire_view_signals()
        self._bind_service(self._service)
        self._bind_trash(self._trash)

        # ---- 首次从数据库加载 ----
        self.reload()

    # ------------------------------------------------------------------ API
    @property
    def view(self) -> "IngestSourceView":
        return self._view

    @property
    def model(self) -> CardListModel:
        return self._model

    @property
    def service(self) -> Optional["SourceService"]:
        return self._service

    def set_service(self, service: Optional["SourceService"]) -> None:
        """运行时绑定 / 更换服务。

        - 会先断开旧服务的 scan_finished，再连上新的；
        - 传 None 只解绑，不重新加载（清空列表交给 reload 的调用方决定）。
        """
        if self._service is service:
            return
        self._unbind_service(self._service)
        self._service = service
        self._bind_service(service)
        self.reload()

    # ------------------------------------------------------- manager 生命周期
    def setup(self, container) -> None:
        """manager 调用：从 container 补注入依赖。幂等。

        构造期已经拿到了 view（manager 能构造出本实例说明这条链是通的），
        这里只补 container 提供的服务 —— 若构造时已经注入，则近似 no-op；
        若构造时未注入，这里补上并立即 reload。
        """
        if self._service is None:
            service = container.get("source_service", None)
            if service is not None:
                self._service = service
                self._bind_service(service)
                self.reload()

        if self._trash is None:
            self._trash = container.get("source_trash_service", None)
            self._bind_trash(self._trash)

    def teardown(self) -> None:
        """manager 调用：断开信号、清空会话状态。幂等。

        不销毁 view / model —— 它们的生命周期由 Qt 父子关系管理，
        显式 dispose 只会和 Qt 的所有权模型打架。
        """
        if self._torn_down:
            return
        self._torn_down = True

        self._unbind_service(self._service)
        self._unbind_trash(self._trash)

        # 丢弃未 apply 的草稿与会话指针，避免误用
        self._draft_dto = None
        self._current_source_id = None
        self._row_source_ids = []

    # ------------------------------------------------------------ 服务绑定
    def _bind_service(self, service: Optional["SourceService"]) -> None:
        """订阅源码扫描完成（列表无需刷新，仅用于观测）。"""
        if service is None or self._scan_bound:
            return
        try:
            service.scan_finished.connect(self._on_scan_finished_backend)
            self._scan_bound = True
        except Exception:
            _logger.debug("service.scan_finished 订阅失败（忽略）", exc_info=True)

    def _unbind_service(self, service: Optional["SourceService"]) -> None:
        if service is None or not self._scan_bound:
            return
        try:
            service.scan_finished.disconnect(self._on_scan_finished_backend)
        except Exception:
            _logger.debug("service.scan_finished 退订失败（忽略）", exc_info=True)
        self._scan_bound = False

    def _bind_trash(self, trash_service: Optional["SourceTrashService"]) -> None:
        """订阅回收站的源变更（软删 / 恢复 / 清空）。"""
        if trash_service is None or self._trash_bound:
            return
        try:
            trash_service.sources_changed.connect(self._on_sources_changed_backend)
            self._trash_bound = True
        except Exception:
            _logger.debug("service.sources_changed 订阅失败（忽略）", exc_info=True)

    def _unbind_trash(self, trash_service: Optional["SourceTrashService"]) -> None:
        if trash_service is None or not self._trash_bound:
            return
        try:
            trash_service.sources_changed.disconnect(
                self._on_sources_changed_backend,
            )
        except Exception:
            _logger.debug("service.sources_changed 退订失败（忽略）", exc_info=True)
        self._trash_bound = False

    # ------------------------------------------------------------ 数据加载
    def reload(self) -> None:
        """从数据库全量拉取导入源，刷新卡片列表。"""
        items: list[CardItem] = []
        ids: list[int] = []

        # 草稿固定放第 0 行
        if self._draft_dto is not None:
            items.append(self._dto_to_card_item(self._draft_dto))
            ids.append(DRAFT_SOURCE_ID)

        if self._service is not None:
            try:
                dtos = self._service.list_sources()
            except Exception:
                _logger.exception("加载导入源列表失败")
                dtos = []
            for dto in dtos:
                items.append(self._dto_to_card_item(dto))
                ids.append(dto.id)

        self._row_source_ids = ids
        self._model.set_items(items)

        if (self._current_source_id is not None
                and self._current_source_id not in ids):
            self._current_source_id = None
            self._clear_detail()

    # ------------------------------------------------------------ 映射
    @staticmethod
    def _dto_to_card_item(dto: IngestSource) -> CardItem:
        """IngestSource -> CardItem 的纯函数映射。"""
        return CardItem(
            title=dto.display_name,  # DTO 自带 display_name 兜底
            description=dto.description or "",
            path=dto.source_path or "",
            tags=list(dto.tags or []),
        )

    def _dto_for_id(self, source_id: int) -> Optional[IngestSource]:
        if self._service is None:
            return None
        try:
            return self._service.get_source(source_id)
        except Exception:
            _logger.exception("查询导入源失败 (id=%s)", source_id)
            return None

    # ------------------------------------------------------------ 信号连线
    def _wire_view_signals(self) -> None:
        # 顶部 ✕ 的关闭动作由 WindowPresenter 接（overlay.show_blank），
        # 这里不再重复接一条空槽。
        self._view.card_clicked.connect(self._on_card_clicked)
        self._view.add_button.clicked.connect(self._on_add_requested)
        self._view.apply_button.clicked.connect(self._on_apply_requested)
        self._view.detail_closed.connect(self._on_detail_closed)
        self._view.context_menu_requested.connect(self._on_context_menu_requested)

    # ------------------------------------------------------------ 交互回调
    def _on_card_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if row < 0 or row >= len(self._row_source_ids):
            return
        source_id = self._row_source_ids[row]
        self._current_source_id = source_id

        # 草稿：喂内存 DTO
        if source_id == DRAFT_SOURCE_ID:
            if self._draft_dto is not None:
                self._populate_detail(self._draft_dto)
            return

        dto = self._dto_for_id(source_id)
        if dto is None:
            # 源已消失（被别处删除）→ 全量刷新
            self.reload()
            return
        self._populate_detail(dto)

    def _populate_detail(self, dto: IngestSource) -> None:
        self._view.detail_widget.set_form_data(dto.to_form_data())

    def _clear_detail(self) -> None:
        self._view.detail_widget.clear()

    # ------------------------------------------------------------------
    # 写操作（供 view / 外部调用）
    # ------------------------------------------------------------------
    def create_source(self, payload: IngestSourceCreate) -> Optional[int]:
        """新增导入源，返回新建 id；失败 / 服务未就绪返回 None。"""
        if self._service is None:
            _logger.warning("未注入 SourceService，忽略 create_source")
            return None
        try:
            new_id = self._service.create_source(payload)
        except Exception:
            _logger.exception("创建导入源失败")
            return None
        # service 在关闭状态下返回 None；契约已是 Optional[int]
        if new_id is None:
            return None
        self.reload()
        return new_id

    def update_source(self, source_id: int,
                      patch: IngestSourceUpdate) -> bool:
        """按 id 局部更新（仅非 UNSET 字段生效）。"""
        if self._service is None:
            return False
        try:
            ok = self._service.update_source_config(source_id, patch)
        except Exception:
            _logger.exception("更新导入源失败 (id=%s)", source_id)
            return False
        if ok:
            self.reload()
            self._refresh_detail_if_current(source_id)
        return ok

    def delete_source(self, source_id: int) -> bool:
        """删除导入源（虚拟根会被服务层拒绝）。"""
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("拒绝删除虚拟根 source(id=0)")
            return False

        if self._trash is not None:
            try:
                ok = self._trash.delete_source(source_id)
            except ValueError:
                # MANUAL_SOURCE_ID 已在上面挡掉，走到这里说明状态不一致
                _logger.warning("trash 拒绝删除 source(id=%s)", source_id)
                return False
            except Exception:
                _logger.exception("回收站删除失败 (id=%s)", source_id)
                return False
        elif self._service is not None:
            try:
                ok = self._service.delete_source(source_id)
            except Exception:
                _logger.exception("删除导入源失败 (id=%s)", source_id)
                return False
        else:
            return False

        if ok:
            if source_id == self._current_source_id:
                self._current_source_id = None
                self._clear_detail()
            self.reload()
        return ok

    def set_source_disabled(self, source_id: int, disabled: bool) -> bool:
        """显式设置源的启用 / 禁用状态。

        供菜单、快捷键或程序化调用使用；不依赖当前 DTO 的 disabled 值。
        禁用后 service 会立刻把该源从调度跟踪集里剔除，但不删除 DB 行。
        """
        if self._service is None:
            return False
        try:
            if disabled:
                ok = self._service.disable_source(source_id)
            else:
                ok = self._service.enable_source(source_id)
        except Exception:
            _logger.exception(
                "切换导入源禁用状态失败 (id=%s, disabled=%s)",
                source_id, disabled,
            )
            return False
        if ok:
            self.reload()
            self._refresh_detail_if_current(source_id)
        return ok

    def _refresh_detail_if_current(self, source_id: int) -> None:
        """详情页正看着这条源时，把它从库里重新拉一遍。"""
        if source_id != self._current_source_id:
            return
        dto = self._dto_for_id(source_id)
        if dto is not None:
            self._populate_detail(dto)

    # ------------------------------------------------------------ 表单桥
    @staticmethod
    def _form_to_update_patch(form: IngestSourceFormData) -> IngestSourceUpdate:
        """把表单快照组装成 PATCH DTO（全字段显式设置）。

        ``IngestSourceUpdate`` 是 PATCH 语义（默认 UNSET），而表单是完整快照，
        因此这里对每个字段都显式赋值。是否真正"变更"由 service 层的
        ``_source_changed`` 决定 —— 展示字段（title/tags/…）变化不会触发重扫。

        注意：表单**不**控制 ``disabled``，因此这里也不设置 —— 禁用走
        ``set_source_disabled``（菜单项），语义上是列表级的开关，不是详情页
        的配置字段。
        """
        return IngestSourceUpdate(
            title=form.title or "",
            description=form.description or None,
            source_path=form.source_path or "",
            target_path=form.target_path,
            mount_point=form.mount_point,
            auto_mount=form.auto_mount,
            file_type_check=form.file_type_check,
            file_types=list(form.file_types),
            tags=list(form.tags),
            subfolder_recursion=form.subfolder_recursion,
            subfolder_recursion_depth=form.subfolder_recursion_depth,
            trigger_config=form.to_trigger_config(),
        )

    @staticmethod
    def _form_to_create_payload(
            form: IngestSourceFormData, fallback_title: str,
    ) -> IngestSourceCreate:
        """表单快照 → 新建 DTO。"""
        return IngestSourceCreate(
            title=form.title or fallback_title,
            description=form.description or None,
            source_path=form.source_path or "",
            target_path=form.target_path,
            mount_point=form.mount_point,
            auto_mount=form.auto_mount,
            file_type_check=form.file_type_check,
            file_types=list(form.file_types),
            tags=list(form.tags),
            subfolder_recursion=form.subfolder_recursion,
            subfolder_recursion_depth=form.subfolder_recursion_depth,
            trigger_config=form.to_trigger_config(),
        )

    # ------------------------------------------------------------ 按钮回调
    def _on_add_requested(self) -> None:
        """顶部 + ：造一张内存草稿，直接跳进详情页编辑。"""
        if self._service is None:
            return

        if self._draft_dto is not None:
            self._focus_draft_row()
            return

        self._draft_dto = IngestSource(
            id=DRAFT_SOURCE_ID,
            title=self.tr("未命名导入源"),
            description=None,
            source_path="",
            target_path=None,
            mount_point=None,
            auto_mount=False,
            file_type_check=FileTypeCheckMode.SUFFIX,
            file_types=[],
            tags=[],
            subfolder_recursion=False,
            subfolder_recursion_depth=None,
            trigger_config=None,
            disabled=False,
        )
        self.reload()
        self._focus_draft_row()

    def _focus_draft_row(self) -> None:
        if not self._row_source_ids or self._row_source_ids[0] != DRAFT_SOURCE_ID:
            return
        self._open_row(0)

    def _focus_source_by_id(self, source_id: int) -> None:
        try:
            row = self._row_source_ids.index(source_id)
        except ValueError:
            return
        self._open_row(row)

    def _open_row(self, row: int) -> None:
        """让 view 打开第 ``row`` 张卡片（没有 open_card 时退化成点击）。"""
        idx = self._model.index(row, 0)
        opener = getattr(self._view, "open_card", None)
        if callable(opener):
            opener(idx)
        else:
            self._on_card_clicked(idx)

    def _on_apply_requested(self) -> None:
        """详情页"应用"：草稿态 create，普通态 patch。"""
        if self._service is None or self._current_source_id is None:
            return

        form = self._view.detail_widget.get_form_data()

        if self._current_source_id == DRAFT_SOURCE_ID:
            self._create_from_draft(form)
            return

        self.update_source(
            self._current_source_id, self._form_to_update_patch(form),
        )

    def _create_from_draft(self, form: IngestSourceFormData) -> None:
        """把草稿表单提交为一条新记录，成功后定位到新卡片。"""
        payload = self._form_to_create_payload(form, self.tr("未命名导入源"))
        try:
            new_id = self._service.create_source(payload)
        except Exception:
            _logger.exception("创建导入源失败")
            return
        if new_id is None:
            return

        self._draft_dto = None
        self._current_source_id = new_id
        self.reload()
        self._focus_source_by_id(new_id)

    # ------------------------------------------------------------ 后端事件
    def _on_scan_finished_backend(self, _sender, *, event) -> None:
        """后端 SourceExecutor 在工作线程里回调。

        契约：``cb(sender: SourceExecutor, *, event: SourceScanFinished)``
        （见 ``albuswall.utils.signals``）——sender 不关心，payload 具名。

        绝不在此触碰 UI —— 只是把事件搬到主线程。
        """
        self._scan_finished_queued.emit(event)

    @staticmethod
    def _on_scan_finished_on_ui(event) -> None:
        """在主线程里消费扫描完成事件（当前只观测，不刷新列表）。"""
        if getattr(event, "skipped", False):
            _logger.debug(
                "source(id=%s) 已删除/失效，扫描跳过: seq=%s",
                getattr(event, "source_id", None),
                getattr(event, "task_seq", None),
            )
            return
        _logger.debug(
            "source(id=%s) 扫描完成: scanned=%s new=%s has_new=%s",
            getattr(event, "source_id", None),
            getattr(event, "scanned_file_count", None),
            getattr(event, "new_file_count", None),
            getattr(event, "has_new_content", None),
        )

    def _on_detail_closed(self) -> None:
        """关闭详情时，未 apply 的草稿直接丢弃。"""
        if self._draft_dto is None:
            return
        self._draft_dto = None
        self._current_source_id = None
        self.reload()

    def _on_context_menu_requested(self, index: QModelIndex, global_pos) -> None:
        row = index.row()
        if row < 0 or row >= len(self._row_source_ids):
            return
        source_id = self._row_source_ids[row]

        # 草稿行 / 虚拟根不参与 删除·禁用
        if source_id in (DRAFT_SOURCE_ID, MANUAL_SOURCE_ID):
            return

        self._exec_context_menu(source_id, global_pos)

    def _exec_context_menu(self, source_id: int, global_pos) -> None:
        """两态循环：

            普通态: [删除]  [禁用/启用]
            确认态: [恢复]  [确认删除]
        """
        dto = self._dto_for_id(source_id)
        currently_disabled = bool(getattr(dto, "disabled", False))
        toggle_label = (
            self.tr("enable") if currently_disabled else self.tr("disable")
        )

        confirm_mode = False
        while True:
            menu = QMenu(self._view)
            if confirm_mode:
                act_restore = menu.addAction(self.tr("recover"))
                act_confirm = menu.addAction(self.tr("ensure deleted"))
            else:
                act_delete = menu.addAction(self.tr("delete"))
                act_toggle = menu.addAction(toggle_label)

            chosen = menu.exec(global_pos)
            if chosen is None:
                return

            if not confirm_mode:
                if chosen is act_delete:
                    confirm_mode = True
                    continue
                if chosen is act_toggle:
                    # 无状态 toggle：交给 service 按当前 DTO 决定方向
                    self._on_disable_requested(source_id)
                    return
                continue

            if chosen is act_restore:
                confirm_mode = False
                continue
            if chosen is act_confirm:
                self.delete_source(source_id)
                return
            return

    def _on_disable_requested(self, source_id: int) -> None:
        """禁用 / 启用导入源 —— 状态存 DB 列 ``ingest_source.disabled``。

        无状态 toggle：读当前 DTO 的 ``disabled`` 决定方向，然后调
        ``enable_source`` / ``disable_source``。禁用后 service 会立刻把该源
        从调度跟踪集里剔除，DB 行保留（禁用 ≠ 删除）。
        """
        dto = self._dto_for_id(source_id)
        if dto is None:
            _logger.debug("切换禁用状态时源已不存在 (id=%s)", source_id)
            return
        self.set_source_disabled(source_id, not dto.disabled)

    # ---- 后端线程调用：只 emit，绝不碰 UI ----
    def _on_sources_changed_backend(self, _sender, **_kwargs) -> None:
        """数据变更 → 转 Qt 信号搬回主线程。

        * 契约是 ``cb(sender, *, source_id, kind)``；本槽不关心 sender 与
          载荷（``_sender`` / ``**_kwargs`` 显式声明"我不关心"）。
        * emit 会因线程不同自动走 QueuedConnection，槽函数在 UI 线程执行。
        """
        self._refresh_queued.emit()

    # ---- 主线程槽：安全刷新 ----
    def _on_refresh_on_ui(self) -> None:
        self.reload()
