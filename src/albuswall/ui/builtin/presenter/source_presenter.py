#
""""""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional, Final

from PySide6.QtCore import QObject, QModelIndex
from PySide6.QtWidgets import QMenu

from albuswall.ui.builtin.model.source_card import CardItem, CardListModel
from albuswall.ui.builtin.delegate.source_card import CardItemDelegate
from albuswall.common.enums import FileTypeCheckMode
from albuswall.dto.source import (
    MANUAL_SOURCE_ID,
    IngestSourceCreate,
    IngestSourceFormData,
    IngestSourceUpdate,
    IngestSourceViewDTO,
)

DRAFT_SOURCE_ID: Final[int] = -1

if TYPE_CHECKING:
    from ..window.source.core import IngestSource
    from albuswall.services.source import SourceFacedService  # 依据实际模块路径调整

_logger = logging.getLogger(__name__)


# noinspection broad-exception
class IngestSourcePresenter(QObject):
    """IngestSource 的 presenter。

    职责：
      * 创建并持有 model / delegate，把它们装配到 view 上
      * 把 view 的信号翻译成业务动作（增删改查 → SourceFacedService）
      * 作为唯一的“数据入口” —— 所有对外交互都经 SourceFacedService，
        视图层对 repository / DB 完全无感知。
    """

    def __init__(
        self,
        view: "IngestSource",
        source_service: Optional["SourceFacedService"] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)

        self._draft_dto: Optional[IngestSourceViewDTO] = None
        # ★ 内存里的禁用状态（TODO：后续落库到 ingest_source.disabled）
        self._disabled_source_ids: set[int] = set()

        # ---- 视图 ----
        self._view = view

        # ---- 业务服务（唯一数据入口） ----
        self._service: Optional["SourceFacedService"] = source_service

        # ---- 数据层 ----
        self._model = CardListModel(parent=self)
        # delegate 的 parent 传 view.view（即实际渲染的 ColumnListView），
        # 这样 delegate 能通过 option.widget 读到 QSS 上的卡片参数
        self._delegate = CardItemDelegate(self._view.view)

        self._view.set_model(self._model, self._delegate)

        # ---- 会话状态 ----
        # row -> source_id 映射；顺序与 model 中 item 顺序一一对应。
        self._row_source_ids: List[int] = []
        # 当前被选中的 source_id（详情面板对应源）。
        self._current_source_id: Optional[int] = None

        # ---- 连线 ----
        self._wire_signals()

        # ---- 首次从数据库加载 ----
        self.reload()

    # ------------------------------------------------------------------ API
    @property
    def view(self) -> "IngestSource":
        """供外部挂到父窗口使用的 widget。"""
        return self._view

    @property
    def model(self) -> CardListModel:
        """暴露模型以便上层做细粒度操作（如需）。"""
        return self._model

    @property
    def service(self) -> Optional["SourceFacedService"]:
        return self._service

    def set_service(self, service: "SourceFacedService") -> None:
        """运行时绑定服务（例如由 main window 在 DI 完成后注入）。"""
        self._service = service
        self.reload()

    # ------------------------------------------------------------ 数据加载
    def reload(self) -> None:
        """从数据库全量拉取导入源，刷新卡片列表。

        - 使用 ``SourceFacedService.list_sources()`` 拿视图 DTO；
        - 每次 reload 会重建 row -> id 映射，避免删除/插入后错位。
        """
        items: List[CardItem] = []
        ids: List[int] = []

        # ★ 草稿固定放第 0 行
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
    def _dto_to_card_item(dto: IngestSourceViewDTO) -> CardItem:
        """IngestSourceViewDTO -> CardItem 的纯函数映射。

        如需在卡片上展示 auto_mount / file_types 等信息，
        可扩展 CardItem 后再补字段即可（本函数是唯一映射点）。
        """
        title = dto.title or dto.source_path or f"source#{dto.id}"
        return CardItem(
            title=title,
            description=dto.description or "",
            path=dto.source_path or "",
            tags=list(dto.tags or []),
        )

    # ------------------------------------------------------------ 信号连线
    def _wire_signals(self) -> None:
        # 卡片点击 -> 详情
        self._view.card_clicked.connect(self._on_card_clicked)
        # 顶部三个按钮
        self._view.add_button.clicked.connect(self._on_add_requested)
        self._view.apply_button.clicked.connect(self._on_apply_requested)
        self._view.close_button.clicked.connect(self._on_close_requested)
        self._view.detail_closed.connect(self._on_detail_closed)
        self._view.context_menu_requested.connect(self._on_context_menu_requested)

        # 若后端暴露 scan_finished 信号，可在此桥接到本 presenter：
        if self._service is not None:
            try:
                self._service.scan_finished.connect(self._on_scan_finished)
            except Exception:
                # 未实现 / 未注入信号时静默跳过
                _logger.debug("service.scan_finished 订阅失败（忽略）",
                              exc_info=True)

    # ------------------------------------------------------------ 交互回调
    def _on_card_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if row < 0 or row >= len(self._row_source_ids):
            return
        source_id = self._row_source_ids[row]
        self._current_source_id = source_id

        # ★ 草稿：喂内存 DTO
        if source_id == DRAFT_SOURCE_ID:
            if self._draft_dto is not None:
                self._populate_detail(self._draft_dto)
            return

        if self._service is None:
            return
        dto = self._service.get_source(source_id)
        if dto is None:
            self.reload()
            return
        self._populate_detail(dto)

    # noinspection calling-non-callable
    def _populate_detail(self, dto: IngestSourceViewDTO) -> None:
        detail = self._view.detail_widget
        setter = getattr(detail, "set_form_data", None)
        if callable(setter):
            setter(dto.to_form_data())
            return
        # 兼容老接口（可删）
        setter = getattr(detail, "set_source_dto", None)
        if callable(setter):
            setter(dto)

    # noinspection calling-non-callable
    def _clear_detail(self) -> None:
        detail = self._view.detail_widget
        setter = getattr(detail, "clear", None)
        if callable(setter):
            setter()

    # ------------------------------------------------------------------
    # 写操作（供 view / 外部调用）
    # ------------------------------------------------------------------
    def create_source(self, payload: IngestSourceCreate) -> Optional[int]:
        """新增导入源，返回新建 id；失败返回 None。"""
        if self._service is None:
            _logger.warning("未注入 SourceFacedService，忽略 create_source")
            return None
        try:
            new_id = self._service.create_source(payload)
        except Exception:
            _logger.exception("创建导入源失败")
            return None
        self.reload()
        return new_id

    def update_source(self, source_id: int,
                      patch: IngestSourceUpdate) -> bool:
        """按 id 局部更新（仅非 None 字段生效）。"""
        if self._service is None:
            return False
        try:
            ok = self._service.update_source_config(source_id, patch)
        except Exception:
            _logger.exception("更新导入源失败 (id=%s)", source_id)
            return False
        if ok:
            # 若改的是当前选中项，顺手刷新详情
            self.reload()
            if source_id == self._current_source_id:
                dto = self._service.get_source(source_id)
                if dto is not None:
                    self._populate_detail(dto)
        return ok

    def delete_source(self, source_id: int) -> bool:
        """删除导入源（虚拟根会被服务层拒绝）。"""
        if self._service is None:
            return False
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("拒绝删除虚拟根 source(id=0)")
            return False
        try:
            ok = self._service.delete_source(source_id)
        except Exception:
            # 常见：assets 外键 RESTRICT 触发 DB 异常
            _logger.exception("删除导入源失败 (id=%s)", source_id)
            return False
        if ok:
            if source_id == self._current_source_id:
                self._current_source_id = None
                self._clear_detail()
            self.reload()
        return ok

    # ------------------------------------------------------------ 按钮回调
    def _on_add_requested(self) -> None:
        """顶部 + ：造一张内存草稿，直接跳进详情页编辑。"""
        if self._service is None:
            return

        # 已有草稿：直接聚焦，不重复造
        if self._draft_dto is not None:
            self._focus_draft_row()
            return

        self._draft_dto = IngestSourceViewDTO(
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
        )
        self.reload()
        self._focus_draft_row()

    def _focus_draft_row(self) -> None:
        if not self._row_source_ids or self._row_source_ids[0] != DRAFT_SOURCE_ID:
            return
        idx = self._model.index(0, 0)
        # 直接调 view 的公开入口，让它切布局 + emit card_clicked
        opener = getattr(self._view, "open_card", None)
        if callable(opener):
            opener(idx)
        else:
            self._on_card_clicked(idx)

    def _focus_source_by_id(self, source_id: int) -> None:
        try:
            row = self._row_source_ids.index(source_id)
        except ValueError:
            return
        idx = self._model.index(row, 0)
        opener = getattr(self._view, "open_card", None)
        if callable(opener):
            opener(idx)
        else:
            self._on_card_clicked(idx)

    def _on_apply_requested(self) -> None:
        if self._service is None or self._current_source_id is None:
            return

        detail = self._view.detail_widget
        getter = getattr(detail, "get_form_data", None)
        if not callable(getter):
            _logger.debug("detail_widget 未提供 get_form_data()，apply 忽略")
            return

        form: IngestSourceFormData = getter()

        # ★ 草稿态 → create
        if self._current_source_id == DRAFT_SOURCE_ID:
            payload = IngestSourceCreate(
                title=form.title or self.tr("未命名导入源"),
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
            return

        # 普通态 → patch
        patch = IngestSourceUpdate.from_form_data(form)
        self.update_source(self._current_source_id, patch)

    def _on_close_requested(self) -> None:
        """顶部 “✕”：关闭 / 从父容器移除本页面。

        纯 UI 动作，无需触碰数据库。
        """
        # TODO: 与上层容器或路由协商关闭行为。
        pass

    # ------------------------------------------------------------ 后端事件
    def _on_scan_finished(self, event) -> None:
        """单个 source 扫描完成时由后端线程 emit。

        注意：本回调运行在执行线程中；如需更新 UI，请用
        ``QMetaObject.invokeMethod`` / 信号排队切回主线程。
        """
        # 例：只做日志；如需刷新列表请切回主线程后再 self.reload()
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
        if source_id == DRAFT_SOURCE_ID or source_id == MANUAL_SOURCE_ID:
            return

        # 两态循环：普通态 <-> 确认态
        #   普通态: [删除]        [禁用]
        #   确认态: [恢复]        [确认删除]
        # 点击“删除”进入确认态；点击“恢复”回到普通态。
        confirm_mode = False
        while True:
            menu = QMenu(self._view)

            if not confirm_mode:
                act_delete = menu.addAction(self.tr("delete"))
                act_disable = menu.addAction(self.tr("disable"))
            else:
                act_restore = menu.addAction(self.tr("recover"))
                act_confirm = menu.addAction(self.tr("ensure deleted"))

            chosen = menu.exec(global_pos)
            if chosen is None:
                # 用户点了菜单外面，直接结束
                return

            if not confirm_mode:
                if chosen is act_delete:
                    confirm_mode = True
                    continue  # 重新弹一次，显示确认态
                if chosen is act_disable:
                    self._on_disable_requested(source_id)
                    return
            else:
                if chosen is act_restore:
                    confirm_mode = False
                    continue  # 回到普通态
                if chosen is act_confirm:
                    self.delete_source(source_id)
                    return

    def _on_disable_requested(self, source_id: int) -> None:
        """禁用 / 启用导入源。

        NOTE: 目前 IngestSourceViewDTO / 表结构里还没有 ``disabled`` 字段，
              这里暂存在内存 set 中，仅用于打通交互；后续需要在
              `ingest_source` 加列、DTO 加字段后再改成落库 + reload。
        """
        # TODO: 落库到 ingest_source.disabled 并刷新卡片样式
        if source_id in self._disabled_source_ids:
            self._disabled_source_ids.discard(source_id)
        else:
            self._disabled_source_ids.add(source_id)
        _logger.info(
            "切换源禁用状态 (id=%s, disabled=%s)",
            source_id, source_id in self._disabled_source_ids,
        )


# --------------------------------------------------------------- 使用示例
def create_ingest_source(
    source_service: Optional["SourceFacedService"] = None,
    parent: Optional[QObject] = None,
) -> IngestSourcePresenter:
    """常用工厂：外部一行代码即可拿到挂好真实数据的视图。

    用法::

        svc = SourceFacedService(source_repo, import_repo, task_service)
        presenter = create_ingest_source(svc)
    """
    presenter = IngestSourcePresenter(source_service=source_service, parent=parent)
    return presenter


# if __name__ == "__main__":
#     import sys
#     from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
#
#     # NOTE: 独立运行时需要先构建好 repo / service 链路，此处仅演示 UI 挂载。
#     from albuswall.repositories import (
#         IngestSourceRepository, ImportRepository,
#     )
#     # from albuswall.services.task import TaskService  # 按实际路径导入
#
#     app = QApplication(sys.argv)
#
#     # ---- 组装后端 ----
#     source_repo = IngestSourceRepository()
#     import_repo = ImportRepository()
#     # task_service = TaskService(...)
#     # svc = SourceFacedService(source_repo, import_repo, task_service)
#     svc = None  # 无 service 时列表为空，可观察到空态表现
#
#     window = QWidget()
#     layout = QVBoxLayout(window)
#     layout.setContentsMargins(0, 0, 0, 0)
#
#     presenter = create_ingest_source(svc)
#     layout.addWidget(presenter.view)
#
#     window.resize(1000, 700)
#     window.show()
#     sys.exit(app.exec())
