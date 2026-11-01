#
"""``SourceService`` —— 被动写命令层：只写 ``ingest_source`` 表并发信号。

边界（严格）
------------
**持有**：无运行时状态。

**依赖**：只有 :class:`~albuswall.repositories.source.IngestSourceRepository`。
不 import ``TaskService`` / ``SourceRuntime`` / ``SourceStateService`` ——
"写完之后要重扫 / 重挂载" 是订阅方的事（见包 docstring 的信号拓扑）。

**不**做的三件事（旧门面的越界行为，全部移走）：

* 不再内部触发扫描（旧 ``create_source`` 里那句 ``update_source(new_id)``）；
* 不再持有跟踪集（→ ``SourceStateService.tracked_sources``）；
* 不再持有 ``scan_finished``（→ ``SourceRuntime.scan_finished``）。

信号
----
三个信号都**只带 source_id**，不传快照 —— 订阅方自己从仓储读最新值。
好处是"信号 = 一个事实发生了"，而不是"信号 = 一份可能过期的副本"：

====================  ==================================================
信号                   含义
====================  ==================================================
``source_created``     新行已提交（``create`` 写库成功后）
``source_changed``     ``update`` / ``enable`` / ``disable`` 写库生效
``source_deleted``     ``delete`` 已物理删除该行（含"软删 + purge"路径）
====================  ==================================================
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, TypeVar

from albuswall.dto.source.command import IngestSourceCreate, IngestSourceUpdate
from albuswall.dto.source.query import MANUAL_SOURCE_ID, IngestSource
from albuswall.log import getLogger
from albuswall.repositories.source import IngestSourceRepository
from albuswall.utils.signals import SignalDescriptor

from ..errors import (
    SourceCreateFailed,
    SourceDeleteFailed,
    SourceNotFound,
    SourcePathInvalid,
    SourceServiceClosed,
    SourceUpdateFailed,
    SourceVirtualRoot,
)

if TYPE_CHECKING:  # 只为类型标注；运行时用鸭子类型，避免 import 顺序依赖
    from ...main_loop_service import MainLoopService

__all__ = ["SourceService"]

_logger = getLogger(__name__)

T = TypeVar("T")


class SourceService:
    """导入源的写命令服务（被动：不启动任何后台行为）。

    线程边界
    --------
    ``create`` / ``update`` / ``delete`` 是**跨线程安全**的：它们把整个动作
    （写库 + ``emit``）搬到主循环线程执行，私有实现
    （``_create`` / ``_update`` / ``_delete``）只在主线程跑。原因见
    :mod:`albuswall.service.main_loop_service`：``Signal`` 同步派发，而
    状态层（``TrackingState``）只允许属主线程写 —— 在 IPC 工作线程里
    ``emit`` 会在 DB 已提交之后才炸。

    读路径（``list_sources`` / ``get_source`` / ``has_source`` /
    ``get_sync_candidate`` / ``source_path_is_dir``）**不平移**：纯仓储转发、
    不写任何状态，任何线程都安全，也没必要让读命令陪写命令排队。
    """

    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: SourceService, *, source_id: int)
    source_created = SignalDescriptor("source_created")
    source_changed = SignalDescriptor("source_changed")
    source_deleted = SignalDescriptor("source_deleted")

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            *,
            main_loop: "MainLoopService | None" = None,
    ) -> None:
        self._source_repo = source_repo
        #: 主线程调度器。``None`` = 直接同步执行（单测 / 未装配主循环）。
        self._main_loop = main_loop
        self._closed = False

    # ------------------------------------------------------------------ 线程边界
    def _on_main(self, fn: Callable[[], T]) -> T:
        """把 ``fn`` 送到主循环线程执行（已在该线程时直接跑）。"""
        if self._main_loop is None:
            return fn()
        return self._main_loop.call(fn)

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = f"SourceService(closed={self._closed})"
        if verbose:
            base = base[:-1] + f", repo={type(self._source_repo).__name__})"
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------ 生命周期
    def shutdown(self) -> None:
        """关闭服务：此后**写**调用抛 :class:`SourceServiceClosed`。

        * 幂等；
        * 不清空信号订阅者（订阅生命周期归订阅方，与旧实现一致）；
        * 不碰仓储、不碰 UI 状态；
        * 读路径（``list_sources`` / ``get_source`` / ``has_source`` /
          ``get_sync_candidate``）关闭后照常可用 —— 它们只是转发仓储。
        """
        if self._closed:
            return
        self._closed = True
        _logger.debug("SourceService shut down")

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self, operation: str) -> None:
        if self._closed:
            _logger.debug("write op rejected: SourceService is closed")
            raise SourceServiceClosed(operation)

    # ==================================================================
    # 写
    # ==================================================================
    def create(self, source: IngestSourceCreate) -> int:
        """创建导入源，返回新 id；库写成功后才发 ``source_created``。

        跨线程安全：整段（写库 + 发信号）在主循环线程执行。

        Raises:
            SourceServiceClosed: 服务已关闭。
            SourcePathInvalid: ``source_path`` 为空（未给路径的源无法扫描）。
            SourceCreateFailed: 仓储 INSERT 失败。
            MainLoopUnavailable: 主循环已关闭 / 无法投递。
            MainThreadTimeout: 主循环在超时内没有响应。
        """
        return self._on_main(lambda: self._create(source))

    def _create(self, source: IngestSourceCreate) -> int:
        self._ensure_open("create")

        if not (source.source_path or "").strip():
            # 不校验目录是否存在：源可能在挂载后才可见（auto_mount），
            # 建一个"暂时不成立"的源是合法意图，由 state 层的 is_void 处理。
            # source_id 用 MANUAL_SOURCE_ID(0) 表示"这一行还没落库"。
            raise SourcePathInvalid(
                MANUAL_SOURCE_ID, source.source_path, "source_path is empty"
            )

        try:
            new_id = self._source_repo.create(source)
        except Exception as exc:  # noqa: BLE001 —— 统一收敛成领域异常
            _logger.exception("Failed to create source")
            raise SourceCreateFailed(exc) from exc

        _logger.debug("Created source %d", new_id)
        self.source_created.emit(self, source_id=new_id)
        return new_id

    def update(self, source_id: int, patch: IngestSourceUpdate) -> None:
        """局部更新（PATCH 语义）。

        * ``patch`` 全 ``UNSET`` → 直接返回（没有变更意图，不写库、不发信号）；
        * 写库生效后才发 ``source_changed``；
        * 跨线程安全：整段在主循环线程执行。

        Raises:
            SourceServiceClosed: 服务已关闭。
            SourceNotFound: 该 id 在库中不存在。
            SourceUpdateFailed: 仓储写入未生效 / 抛错。
            MainLoopUnavailable: 主循环已关闭 / 无法投递。
            MainThreadTimeout: 主循环在超时内没有响应。
        """
        self._on_main(lambda: self._update(source_id, patch))

    def _update(self, source_id: int, patch: IngestSourceUpdate) -> None:
        self._ensure_open("update")

        if patch.is_empty():
            _logger.debug("update(%d) ignored: empty patch", source_id)
            return

        if not self._source_repo.exists(source_id):
            raise SourceNotFound(source_id)

        try:
            written = self._source_repo.update(source_id, patch)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Failed to update source %d", source_id)
            raise SourceUpdateFailed(source_id, exc) from exc

        if not written:
            # 已确认行存在却一个字段都没写 —— 只可能是竞态（并发删除）。
            raise SourceUpdateFailed(source_id)

        _logger.debug("Updated source %d", source_id)
        self.source_changed.emit(self, source_id=source_id)

    def delete(self, source_id: int) -> None:
        """删除导入源（物理删除该行）。

        实现说明：仓储层**没有**裸 ``DELETE`` 接口，硬删只有 ``purge``
        一条路（它要求行处于 ``is_deleted=1``，并在一个事务里连资产行一起
        删）。因此顺序是"校验 → 软删 → 立即 purge"，对外仍是"删掉一个源"。

        注意（半完成状态）：本方法只负责 **DB 侧**的删除 —— 源行与旗下资产
        行在同一个 purge 事务里被物理删除，``asset_candidate_cache`` 由外键
        CASCADE 顺带清掉；**磁盘侧的缩略图 PNG 不会在这里被清理**。孤儿缩略图
        的清理（以及"回收站语义：软删 → 异步 purge"这条入口）都归
        ``SourceTrashService`` / ``ThumbnailCleaner``，该服务当前尚未迁出，
        见 ``service/old/__init__.py`` 的迁移清单（搜「半完成」）。
        所以对调用方而言：DB 语义上"删除"已完成，磁盘语义上仍是半完成。

        跨线程安全：整段（校验 + 软删 + purge + 发信号）在主循环线程执行。

        Raises:
            SourceServiceClosed: 服务已关闭。
            SourceVirtualRoot: 目标是虚拟根（``MANUAL_SOURCE_ID``）。
            SourceNotFound: 该 id 在库中不存在。
            SourceDeleteFailed: 仍有资产引用该源（仓储拒绝），
                或软删 / purge 未生效。
            MainLoopUnavailable: 主循环已关闭 / 无法投递。
            MainThreadTimeout: 主循环在超时内没有响应。
        """
        self._on_main(lambda: self._delete(source_id))

    def _delete(self, source_id: int) -> None:
        self._ensure_open("delete")

        if source_id == MANUAL_SOURCE_ID:
            raise SourceVirtualRoot(source_id, operation="delete")

        if not self._source_repo.exists(source_id):
            raise SourceNotFound(source_id)

        # 有资产引用的源不允许删：purge 会连资产行一起删掉，
        # 而"删源顺带删照片"不是用户按一次删除按钮时期望的语义。
        # 想清空一个源请先走回收站 / 资产删除流程。
        if self._source_repo.count_assets(source_id) > 0:
            raise SourceDeleteFailed(
                source_id,
                RuntimeError("source still has assets referencing it"),
            )

        try:
            soft_deleted = self._source_repo.soft_delete(source_id)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Failed to soft-delete source %d", source_id)
            raise SourceDeleteFailed(source_id, exc) from exc

        if not soft_deleted:
            raise SourceDeleteFailed(
                source_id, RuntimeError("soft delete did not take effect")
            )

        try:
            report = self._source_repo.purge(source_id)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Failed to purge source %d", source_id)
            raise SourceDeleteFailed(source_id, exc) from exc

        if not report.purged:
            raise SourceDeleteFailed(
                source_id, RuntimeError("purge did not take effect")
            )

        _logger.debug("Deleted source %d", source_id)
        self.source_deleted.emit(self, source_id=source_id)

    def enable(self, source_id: int) -> None:
        """启用导入源（幂等）。等价于 ``update(disabled=False)``。"""
        self.update(source_id, IngestSourceUpdate(disabled=False))

    def disable(self, source_id: int) -> None:
        """禁用导入源（幂等）。等价于 ``update(disabled=True)``。

        DB 行保留 —— 禁用是逻辑状态，不是删除。跟踪集 / 任务由订阅了
        ``source_changed`` 的 state 层负责剔除。
        """
        self.update(source_id, IngestSourceUpdate(disabled=True))

    # ==================================================================
    # 读（纯转发，不涉及本服务的生命周期）
    # ==================================================================
    def list_sources(self) -> List[IngestSource]:
        """列出所有导入源（含虚拟根 / 手动导入源），按 id 升序。"""
        return self._source_repo.list_view_dtos()

    def get_source(self, source_id: int) -> Optional[IngestSource]:
        """查询单条导入源；不存在返回 ``None``。"""
        return self._source_repo.get_view_dto(source_id)

    def has_source(self, source_id: int) -> bool:
        """快捷判断导入源是否存在。"""
        return self._source_repo.exists(source_id)

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        """单源同步候选（软删 / 虚拟根 / 不存在都返回 ``None``）。"""
        return self._source_repo.get_sync_candidate(source_id)

    def source_path_is_dir(self, source_id: int) -> bool:
        """便捷判断：该源当前配置的 ``source_path`` 是否是个存在的目录。

        纯只读便利方法（UI 的"目录不可用"提示用），不触发任何调度。
        """
        source = self.get_source(source_id)
        if source is None:
            return False
        path = (source.source_path or "").strip()
        return bool(path) and Path(path).is_dir()
