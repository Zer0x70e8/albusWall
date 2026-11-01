#
"""``SourceStateService`` —— 运行时数据层：源的**有效性判定 + 挂载 + 跟踪集**。

职责
----
它是 :class:`~albuswall.service.source.runtime.service.SourceRuntime`
**唯一的数据入口**：只有本层知道"哪些源该被扫描"，并把它翻译成 runtime
的四类对账计划（``added`` / ``rebuild`` / ``snapshot`` / ``removed``）。

具体做四件事：

1. 从 :class:`~albuswall.repositories.source.IngestSourceRepository` 读源
   配置，用 :meth:`IngestSource.is_void` 判定有效性（虚拟根 / ``disabled`` /
   目录消失 / 挂载点非法都会被判为不可用）；
2. 有效源在首次跟踪 / 扫描行为变更时执行 ``auto_mount``，失败则不跟踪；
3. 维护 :class:`~albuswall.service.source.state.tracking.TrackingState`
   （唯一可变状态）；
4. 把变化分类后调 ``runtime.reconcile(...)``（全量）或 ``runtime.rescan``
   （单源强制重扫）。

**不做**：不写 ``ingest_source``（那是 command 层）、不建 executor / 不提交
任务（那是 runtime 层）、不管触发器配置（那是 trigger 层）。

数据流
------
::

    source_created ─┐
    source_changed ─┼─→ _reconcile_one ─┐
    source_deleted ─┤                   ├─→ runtime.reconcile(...)
    trash_changed ──┘                   │
    start() ─→ _apply_full_snapshot ────┘
    trigger_refresh_signal ─→ refresh_source ─→ _reconcile_one + runtime.rescan

线程契约
--------
公开入口在**主循环线程**执行 —— 由本服务的 ``_on_main`` 保证，不再"假设
调用方在主线程"：

* ``refresh_source`` —— ``trigger_refresh_signal`` 由触发器调度线程发射；
* ``mount_source``   —— 管理端点 / 命令入口。

信号槽（``_on_created`` / ``_on_changed`` / ``_on_deleted`` /
``_on_trash_changed``）由 ``SourceService`` 的写入口保证在主线程发射
（``Signal`` 同步派发），因此这些槽**不再**加一层排队。
``trash_changed`` 的 ``purged`` 分支跑在 reconciler 工作线程上：源行早已被
物理删除、本层也已把它踢出跟踪集，:meth:`_on_trash_changed` 直接 return，
绝不跨线程写 state。
"""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Callable
from pathlib import Path
from pprint import pformat
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Dict, List, Mapping, Optional, Set, Tuple, TypeVar,
)

from albuswall.dto.source.query import MANUAL_SOURCE_ID, IngestSource
from albuswall.log import getLogger
from albuswall.repositories.source import IngestSourceRepository
from albuswall.utils.signals import Signal

from ..errors import SourceMountFailed, SourceNotFound, SourcePathInvalid
from ..runtime.service import SourceRuntime
from .tracking import TrackingState

if TYPE_CHECKING:  # 只为类型标注；运行时用鸭子类型，避免 import 顺序依赖
    from ...main_loop_service import MainLoopService

__all__ = ["MOUNT_TIMEOUT", "SourceStateService"]

_logger = getLogger(__name__)

T = TypeVar("T")

#: 自动挂载命令的超时（秒）。
#:
#: 挂载跑在主线程的写路径里（信号回调 → ``_reconcile_one`` →
#: ``_ensure_mounted`` → ``_auto_mount``），**没有超时就会把 GUI 挂死**：
#: 网络盘 / 失联的 ``mount`` 可以无限期不返回。超时按 ``SourceMountFailed``
#: 处理，用户看到的是"挂载失败"，而不是"应用卡住"。
MOUNT_TIMEOUT = 30.0

# ``_apply_full_snapshot`` 的返回形态：四类变更。
ReconcilePlan = Tuple[
    Dict[int, IngestSource],   # added
    Dict[int, IngestSource],   # rebuild
    Dict[int, IngestSource],   # snapshot
    Set[int],                  # removed_ids
]


class SourceStateService:
    """源配置 → runtime 计划的翻译层。"""

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            runtime: SourceRuntime,
            *,
            source_created: Signal,
            source_changed: Signal,
            source_deleted: Signal,
            trash_changed: Signal,
            main_loop: "MainLoopService | None" = None,
    ) -> None:
        self._source_repo = source_repo
        self._runtime = runtime
        #: 主线程调度器。``None`` = 直接同步执行（单测 / 未装配主循环）。
        self._main_loop = main_loop

        self._source_created = source_created
        self._source_changed_signal = source_changed
        self._source_deleted = source_deleted
        self._trash_changed = trash_changed

        # 唯一可变状态：被跟踪源的快照。任务表在 runtime 里，二者不共享。
        self._tracking = TrackingState()

        self._started = False

    # ------------------------------------------------------------------ 线程边界
    def _on_main(self, fn: Callable[[], T]) -> T:
        """把 ``fn`` 送到主循环线程执行（已在该线程时直接跑）。

        为什么需要：``_tracking``（``TrackingState``）只允许属主线程写，而
        本服务的两个 public 入口都**可能**被非主线程调用：

        * :meth:`refresh_source` —— ``trigger_refresh_signal`` 由触发器调度线程
          发射（``infrastructure/trigger``），以前会直接跨线程写 state；
        * :meth:`mount_source` —— 管理端点 / 未来的命令入口。

        信号槽（``_on_created`` 等）**不**加这一层：它们由
        ``SourceService`` 的写入口保证在主线程发射，再加就是重复排队。
        """
        if self._main_loop is None:
            return fn()
        return self._main_loop.call(fn)

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = f"SourceStateService(state={self._tracking})"
        if verbose:
            base = base[:-1] + f", runtime={self._runtime:v})"
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

    # ------------------------------------------------------------------ 视图
    @property
    def tracked_sources(self) -> Mapping[int, IngestSource]:
        """被跟踪源的只读视图（改不动底表，写路径必须走本服务内部）。"""
        return MappingProxyType(self._tracking.sources)

    def is_tracked(self, source_id: int) -> bool:
        return source_id in self._tracking

    @property
    def tracking(self) -> TrackingState:
        """底层容器（测试 / 诊断用；不要绕过本服务写它）。"""
        return self._tracking

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """全量加载 → 首次对账 → 连接四个信号。

        顺序是刻意的：先 ``reconcile`` 再 ``connect``。启动期间没有任何
        信号可能到达（容器装配尚未完成、UI 还没起来），先算完快照再挂订阅，
        就不会出现"装载到一半被信号打断"的中间态。
        """
        if self._started:
            _logger.warning("SourceStateService already started; ignored")
            return

        latest = self._fetch_latest_valid_sources()
        _logger.debug(
            "Loading sources: valid=%d", len(latest),
        )
        _logger.trace("Latest valid sources: %s", pformat(latest))

        added, rebuild, snapshot, removed_ids = self._apply_full_snapshot(latest)
        self._runtime.reconcile(
            added=added,
            rebuild=rebuild,
            snapshot=snapshot,
            removed_ids=removed_ids,
        )

        self._source_created.connect(self._on_created)
        self._source_changed_signal.connect(self._on_changed)
        self._source_deleted.connect(self._on_deleted)
        self._trash_changed.connect(self._on_trash_changed)

        self._started = True
        _logger.info("SourceStateService started")

    def shutdown(self) -> None:
        """断开信号并清空跟踪集。幂等。

        **不**调 ``runtime.shutdown``：runtime 的拆解顺序由装配层控制
        （state 先拆，runtime 后拆），本服务不越权处置别人的生命周期。
        """
        if not self._started:
            return
        self._source_created.disconnect(self._on_created)
        self._source_changed_signal.disconnect(self._on_changed)
        self._source_deleted.disconnect(self._on_deleted)
        self._trash_changed.disconnect(self._on_trash_changed)
        self._tracking.clear()
        self._started = False
        _logger.info("SourceStateService stopped")

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------ 入口
    def refresh_source(self, source_id: int) -> None:
        """单源刷新：重评估跟踪状态，仍在跟踪则强制重扫一次。

        这是"触发器到点"的落点（``trigger_refresh_signal``）：

        * ``_reconcile_one`` —— 让跟踪集与 DB 对齐（源可能刚被禁用 / 删除）；
        * ``runtime.rescan`` —— 仍在跟踪才换 seq 重扫；源已被剔除时无事可做
          （新建任务由 ``_reconcile_one`` 的 ``added`` 分支负责并已提交）。

        跨线程安全：内部在主循环线程执行（信号可能来自触发器调度线程）。
        """
        self._on_main(lambda: self._refresh_source(source_id))

    def _refresh_source(self, source_id: int) -> None:
        self._reconcile_one(source_id)
        if self.is_tracked(source_id):
            self._runtime.rescan(source_id)

    def mount_source(self, source_id: int) -> None:
        """显式执行一次 ``auto_mount``，失败**抛异常**。

        与自动路径（:meth:`_ensure_mounted`）的区别：自动路径跑在信号回调里，
        异常抛出去会打断 UI 的写路径，所以只记日志、返回 ``False``；
        本方法是给管理端点 / 测试用的"报错版"，便于调用方拿到原因。

        跨线程安全：内部在主循环线程执行（挂载最终会跑 ``subprocess``，属于
        主线程写路径的一部分，见 :data:`MOUNT_TIMEOUT`）。

        Raises:
            SourceNotFound: 源不存在（含虚拟根）。
            SourcePathInvalid: ``source_path`` 为空 / 不是目录。
            SourceMountFailed: 挂载命令失败 / 超时 / 挂载点被占用。
        """
        self._on_main(lambda: self._mount_source(source_id))

    def _mount_source(self, source_id: int) -> None:
        source = self._fetch_source_config(source_id)
        if source is None:
            raise SourceNotFound(source_id)

        path = (source.source_path or "").strip()
        if not path or not Path(path).is_dir():
            raise SourcePathInvalid(source_id, source.source_path, "not a directory")

        if not source.auto_mount:
            return

        try:
            _auto_mount(source)
        except Exception as exc:  # noqa: BLE001 —— 统一收敛成领域异常
            raise SourceMountFailed(
                source_id, source.mount_point, exc
            ) from exc
        _logger.debug(
            "Mounted source %d: %s -> %s",
            source_id, source.mount_point, source.source_path,
        )

    # ------------------------------------------------------------------ 信号槽
    def _on_created(self, _sender: object, *, source_id: int) -> None:
        """``source_created``：新建源 → 评估 + 建任务。"""
        self._reconcile_one(source_id)

    def _on_changed(self, _sender: object, *, source_id: int) -> None:
        """``source_changed``：启用 / 禁用 / 局部更新都走同一条重评估。"""
        self._reconcile_one(source_id)

    def _on_deleted(self, _sender: object, *, source_id: int) -> None:
        """``source_deleted``：源行已删 → 剔除跟踪记录与任务。"""
        self._reconcile_one(source_id)

    def _on_trash_changed(
            self,
            _sender: object,
            *,
            source_id: int,
            kind: str,
    ) -> None:
        """``SourceTrashService.sources_changed``：回收站改了源的可见性。

        * ``soft_delete`` / ``restore``：调用方是 UI 写路径（主线程）。
          以 **DB 当前状态**为准重评估，不区分事件名 —— 软删后
          ``get_sync_candidate`` 返回 ``None``（行被标删），恢复后重新可见，
          两者的分支差异全在 repo 查询里，这里不需要 if。
        * ``purged``：软删的异步续集，跑在 reconciler 的工作线程上。此时源行
          已被物理删除、本层早已不再跟踪它，且**绝不能在非属主线程写 state**
          —— 直接忽略。
        """
        if kind == "purged":
            _logger.trace(
                "Ignore purged event for source %d (worker thread)", source_id
            )
            return
        self._reconcile_one(source_id)

    # ------------------------------------------------------------------ 单源对账
    def _reconcile_one(self, source_id: int) -> None:
        """把单个源的跟踪状态与 DB 对齐，并把差异交给 runtime。"""
        config = self._fetch_source_config(source_id)

        if config is None or config.is_void():
            self._tracking.remove_source(source_id)
            self._runtime.reconcile(removed_ids=(source_id,))
            return

        old = self._tracking.get(source_id)
        needs_task = old is None or self._source_changed(old, config)

        if needs_task:
            # 新增 / 扫描行为变更：需要（重新）挂载
            if not self._ensure_mounted(source_id, config):
                self._tracking.remove_source(source_id)
                # 挂载失败必须显式移除旧任务：否则 runtime 里还留着按**旧**
                # 配置建的执行体，用户会看到"源挂了但还在扫"。
                self._runtime.reconcile(removed_ids=(source_id,))
                return
            self._tracking.upsert_source(source_id, config)
            if old is None:
                self._runtime.reconcile(added={source_id: config})
            else:
                self._runtime.reconcile(rebuild={source_id: config})
            return

        if old != config:
            # 仅展示字段变化：刷新跟踪快照 + 原地刷新任务快照，保留 seq
            self._tracking.upsert_source(source_id, config)
            self._runtime.reconcile(snapshot={source_id: config})
            return

        # 完全未变：保留原对象引用，无操作
        _logger.trace("Source %d unchanged", source_id)

    def _apply_full_snapshot(
            self, latest: Dict[int, IngestSource]
    ) -> ReconcilePlan:
        """把 ``latest`` 与跟踪集 diff，算出四类变更（不调用 runtime）。

        语义与 :meth:`_reconcile_one` 逐条对齐，只是把结果批量收集起来，
        让 ``start()`` 一次 ``reconcile`` 完成。

        * **新增**：挂载成功则跟踪 + ``added``，失败则忽略；
        * **扫描行为变更**（``_source_changed`` 为真）：重挂载，成功则
          ``rebuild``，失败则从跟踪集剔除并**显式** ``removed``；
        * **仅展示字段变更**：刷新快照 + ``snapshot``，不重建任务；
        * **消失**：``removed``。

        未变化的源保留原对象引用，避免下游缓存的 key 被无谓打断。
        """
        current_ids = self._tracking.source_ids
        latest_ids = set(latest)

        added: Dict[int, IngestSource] = {}
        rebuild: Dict[int, IngestSource] = {}
        snapshot: Dict[int, IngestSource] = {}
        removed_ids: Set[int] = set()

        for source_id, new_source in latest.items():
            old = self._tracking.get(source_id)

            if old is None:
                if not self._ensure_mounted(source_id, new_source):
                    continue
                self._tracking.upsert_source(source_id, new_source)
                added[source_id] = new_source
                _logger.trace(
                    "Source %d tracked: %s", source_id, pformat(new_source)
                )
                continue

            if self._source_changed(old, new_source):
                if not self._ensure_mounted(source_id, new_source):
                    self._tracking.remove_source(source_id)
                    removed_ids.add(source_id)
                    continue
                self._tracking.upsert_source(source_id, new_source)
                rebuild[source_id] = new_source
                _logger.trace(
                    "Source %d changed, tracked: %s",
                    source_id, pformat(new_source),
                )
                continue

            if old != new_source:
                # 仅展示字段变了：刷新快照，不重建任务、保留原 seq
                self._tracking.upsert_source(source_id, new_source)
                snapshot[source_id] = new_source
            # else: 完全未变，保留原引用

        stale_ids = current_ids - latest_ids
        if stale_ids:
            _logger.debug(
                "Dropping %d stale source(s): %s",
                len(stale_ids), sorted(stale_ids),
            )
        for source_id in stale_ids:
            self._tracking.remove_source(source_id)
        removed_ids |= stale_ids

        return added, rebuild, snapshot, removed_ids

    # ------------------------------------------------------------------ 读取
    def _fetch_latest_valid_sources(self) -> Dict[int, IngestSource]:
        """从 repo 取所有有效源，返回 ``{id: IngestSource}``。

        ``is_void()`` 已把 ``disabled=True`` 判为不可用，因此这里天然过滤
        掉所有被禁用的源，不需要额外的集合。虚拟根（``MANUAL_SOURCE_ID``）
        显式排除 —— repo 的 ``get_source_candidates`` 虽然已经过滤，
        这里再筛一次：本层不该依赖 repo 的过滤规则。
        """
        candidates = self._source_repo.get_source_candidates()
        return {
            c.id: c
            for c in candidates
            if c.id != MANUAL_SOURCE_ID and not c.is_void()
        }

    def _fetch_source_config(
            self, source_id: int
    ) -> Optional[IngestSource]:
        """单源配置读取；虚拟根显式返回 ``None``。"""
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("Source(id=0) is virtual source, can't be tracked")
            return None
        return self._source_repo.get_sync_candidate(source_id)

    @staticmethod
    def _source_changed(old: IngestSource, new: IngestSource) -> bool:
        """判断需要触发重扫 / 重挂载的字段是否变化。

        比较一切**影响"扫哪些文件 / 扫到哪"的字段**：

        * ``source_path`` / ``target_path`` / ``mount_point`` —— 位置变更；
        * ``file_type_check`` / ``file_types``               —— 筛选规则变更；
        * ``subfolder_recursion`` / ``subfolder_recursion_depth`` —— 深度变更；
        * ``auto_mount``                                     —— 挂载策略变更。

        **筛选规则必须算进来**：``SourceExecutor`` 持有的是创建任务那一刻的
        ``IngestSource`` 快照，任务不重建就一直用旧 DTO 扫。漏掉的后果是
        「把 file_types 从 ``[".png"]`` 补成 ``[".png", ".jpg"]`` 再重扫，
        日志里仍是 ``File globs: ['*.png']``，永远匹配 0 个文件」——
        用户侧表现为"改了过滤条件也没用、库一直是空的"。

        不触发重建的字段：

        * ``title`` / ``description`` / ``tags`` —— 纯展示字段；
        * ``trigger_config``                    —— 调度层字段（归 trigger 层）；
        * ``disabled``                          —— 由 ``is_void`` 短路在上游
          处理（禁用走 drop、启用走新增，都进不了这条分支）。
        """
        return (
                old.target_path != new.target_path
                or old.mount_point != new.mount_point
                or old.source_path != new.source_path
                or old.file_type_check != new.file_type_check
                or list(old.file_types or ()) != list(new.file_types or ())
                or bool(old.subfolder_recursion)
                != bool(new.subfolder_recursion)
                or old.subfolder_recursion_depth
                != new.subfolder_recursion_depth
                or bool(old.auto_mount) != bool(new.auto_mount)
        )

    # ------------------------------------------------------------------ 挂载
    def _ensure_mounted(self, source_id: int, source: IngestSource) -> bool:
        """按需为 source 执行自动挂载（**不抛异常**，只返回 bool）。

        * ``auto_mount=False``：不挂载，视为成功；
        * 挂载异常：记日志（含 ``SourceMountFailed`` 的结构化描述）并返回
          ``False``。信号回调里不能抛 —— 抛出去会打断 UI 的写路径，
          而"挂载失败"本就是可预期的常态（远程目录没连上）。

        需要报错语义的调用方请走 :meth:`mount_source`。
        """
        if not source.auto_mount:
            return True
        try:
            _auto_mount(source)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring
            _logger.error(
                "%s",
                SourceMountFailed(source_id, source.mount_point, exc),
                exc_info=True,
            )
            return False
        _logger.debug(
            "Mounted source %d: %s -> %s",
            source_id, source.mount_point, source.source_path,
        )
        return True


# --------------------------------------------------------------------------- #
# 挂载执行（自 ``utils.mount`` 搬入）
#
# 旧的 ``albuswall.utils.mount`` 随配置层重构被删掉了；挂载能力只服务本层，
# 因此实现收敛在这里，不再单开一个通用 utils 模块（它本来也只有本层一个
# 调用方）。语义与旧实现一致：先查挂载点占用，再校验源目录，最后执行命令。
# --------------------------------------------------------------------------- #

def _ensure_not_mounted(
        mount_point: Path,
        expected_device: str,
) -> None:
    """检查挂载点是否已被占用，若已挂载且设备匹配则直接返回，否则抛异常。"""
    import psutil

    for part in psutil.disk_partitions(all=False):
        if Path(part.mountpoint) != mount_point:
            continue
        if part.device == expected_device:
            _logger.debug(
                "Already mounted at %s with device %s", mount_point, expected_device
            )
            return
        raise RuntimeError(
            f"Mount point {mount_point} is already mounted "
            f"with device {part.device}, expected {expected_device}"
        )


def _build_mount_command(
        mount_point: Path,
        mount_target: str,
        os_name: Optional[str] = None,
) -> List[str]:
    """根据操作系统返回挂载命令列表。"""
    if os_name is None:
        os_name = platform.system().lower()

    if os_name == "windows":
        # Windows 下挂载点必须是盘符（如 Z:）
        mp_str = str(mount_point)
        if not (len(mp_str) == 3 and mp_str[1] == ":"):
            raise RuntimeError(
                "On Windows, mount_point must be a drive letter (e.g., 'Z:')"
            )
        return ["net", "use", mp_str, mount_target]
    if os_name in ("darwin", "linux"):
        return ["mount", mount_target, str(mount_point)]
    raise NotImplementedError(f"Auto-mount not supported on {os_name}")


def _auto_mount(source: IngestSource) -> None:
    """执行自动挂载（``auto_mount=True`` 时前置字段已由 ``is_void`` 校验）。"""
    assert source.mount_point
    assert source.target_path

    mount_point = Path(source.mount_point)
    _ensure_not_mounted(mount_point, source.target_path)

    source_path = Path(source.source_path)
    if not source_path.is_dir():
        raise RuntimeError(
            f"source_path is not a valid directory: {source_path}"
        )

    cmd = _build_mount_command(mount_point, source.target_path)
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            timeout=MOUNT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # 必须自己兜住：挂载跑在主线程的写路径里，无超时会把 GUI 挂死。
        raise RuntimeError(
            f"Mount command timed out after {MOUNT_TIMEOUT}s: {cmd}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Mount command failed: {(exc.stderr or '').strip()}"
        ) from exc
