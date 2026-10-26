#
"""TrashService: soft-deleted asset lifecycle management.

职责
----
* 编排垃圾箱查询 / 恢复 / 清理用例。
* 时钟防护：用 ``trash_state.last_observed_at`` 作为单调水位，缓解系统
  时钟被篡改导致的误删或长期滞留。
* 缩略图文件清理：DB 删除后同步删除 ``thumb_path`` 目录。

不负责
------
* 生成缩略图、写主资产表（属于 ImportService）。
* API 分页、鉴权、审计（属于上层）。
* 孤儿文件扫描（DB 无记录但磁盘残留）：另有专门任务。

时钟防护策略
------------
挂钟时间（wall clock）可被用户和 NTP 重写，而"经过时长"是单调概念；
SQLite 无法持久化单调时钟，跨进程重启无意义。务实做法：

* 维护单调水位 ``last_observed_at``，只增不减。
* 每轮清理读当前时间 ``now`` 与水位 ``watermark``：
    - ``now < watermark``：时钟回拨 → 本轮**不清理**，水位冻结。
    - ``now - watermark > FORWARD_SKEW``：时钟前跳 → 本轮**不清理**，
      水位最多推进 ``ADVANCE_PER_CYCLE``，避免用户拨表一次就误删整库。
    - 其他：正常推进。

效果
----
* 时钟往回拨：绝不提前删除（宁可延迟）。
* 时钟往前拨：本轮不删，水位缓慢追赶；用户保持错误时钟时，清理被持续
  推迟而非触发误删。
* 正常情况：水位每轮与 ``now`` 对齐，清理按期执行。

这是**有意的保守**——宁可垃圾箱滞留，不可错杀。若业务可接受更激进的
语义，调大 ``ADVANCE_PER_CYCLE`` 或放宽 ``FORWARD_SKEW``。
"""

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import partial
from typing import Callable, List, Optional, Sequence, Union
from uuid import UUID

# 注意：``dto.trash`` 也有一个 ``RestoreConflict``（id 面：asset_id +
# file_path），本模块两个都要用——uuid 面那份按别名导入，避免被下面的
# ``from albuswall.dto.trash import ...`` 覆盖。
from albuswall.dto.asset.events import (
    RestoreConflict as UuidRestoreConflict,
    RestoreConflictReason,
    RestoreResult,
)
from albuswall.dto.utils import to_uuid
from albuswall.log import getLogger
from albuswall.utils.time import format_iso, parse_iso
from albuswall.dto.trash import (
    CleanupReport,
    ClockJump,
    PurgeCandidate,
    PurgeReport,
    RestoreConflict,
    RestoreReport,
    TrashedAsset,
    ThumbFailure,
    TrashState,
)
from albuswall.repositories.trash import (
    TrashRepository,
    TrashStateRepository,
)

logger = getLogger(__name__)

# 底层 ``RestoreConflict.reason`` 目前只有 ``"path_conflict"``（唯一索引
# ``idx_assets_source_file_path_active`` 冲突）。向枚举翻译时**不裸调用**
# ``RestoreConflictReason(raw)``——未知取值会让恢复整体抛 ``ValueError``，
# 冲突反而变成崩溃。未知一律降级 ``UNKNOWN``。
_CONFLICT_REASON_MAP = {
    "path_conflict": RestoreConflictReason.PATH_CONFLICT,
    "unique_constraint": RestoreConflictReason.UNIQUE_CONSTRAINT,
    "invalid_state": RestoreConflictReason.INVALID_STATE,
}


def _to_conflict_reason(raw: object) -> RestoreConflictReason:
    """把底层冲突原因（字符串 / 枚举 / None）翻译成领域枚举（永不抛）。"""
    if isinstance(raw, RestoreConflictReason):
        return raw
    if raw is None:
        return RestoreConflictReason.UNKNOWN
    return _CONFLICT_REASON_MAP.get(str(raw), RestoreConflictReason.UNKNOWN)

# ── 策略常量 ────────────────────────────────────────────────────────
TRASH_GRACE = timedelta(days=30)
"""软删除资产的保留期。超过后由 ``cleanup_expired`` 物理清除。"""

FORWARD_SKEW = timedelta(hours=6)
"""判定"时钟前跳"的阈值：``now - watermark`` 超过它即视为可疑。"""

ADVANCE_PER_CYCLE = timedelta(hours=1)
"""检测到前跳后，水位每轮最多推进的时长。"""

PURGE_BATCH_SIZE = 500
"""清理循环的单批大小。过大 → 单事务锁库过久；过小 → 循环开销显著。"""

_FATAL_PURGE_ERRORS: tuple[type[BaseException], ...] = (
    sqlite3.ProgrammingError,  # API 误用 / 连接已关闭：继续循环无意义
    MemoryError,
)
"""清理循环里**允许穿透**的致命异常（其余 ``Exception`` 记为批级失败）。

``sqlite3.ProgrammingError`` 是"用法错误 / 连接不可再用"，重试只会重复
失败；``OperationalError``（锁等待、磁盘错误）属于可恢复的批级故障，
记进 ``PurgeReport.error`` 后停止本轮，下一轮由调度器重试。
"""


# ── 默认时钟实现 ────────────────────────────────────────────────────
def utcnow() -> datetime:
    """默认时钟：返回带 UTC tz 的当前时间。

    生产代码通过容器把它注册为 ``"clock"``；测试可注入任意
    ``Callable[[], datetime]``。公共名字（无下划线）：它是可复用的
    默认实现，不是模块私有细节。
    """
    return datetime.now(timezone.utc)


# 时间字符串格式的唯一来源见 utils/time.py：UTC + 3 位毫秒，
# 与 schema 的 strftime('%Y-%m-%dT%H:%M:%f','now') 同构，
# 可直接参与 ``deleted_at <= ?`` 的字符串比较。


class TrashService:
    """垃圾箱用例编排。

    依赖两个 repo，不持有连接、不做事务——事务边界由 repo 层负责。
    """

    def __init__(
            self,
            trash_repo: TrashRepository,
            state_repo: TrashStateRepository,
            clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._trash = trash_repo
        self._state = state_repo
        self._clock = clock

    # ────────────────────────────────────────────────────────────────
    # 查询（直接透传 repo）
    # ────────────────────────────────────────────────────────────────
    def list_deleted(self, limit: int, offset: int) -> List[TrashedAsset]:
        return self._trash.list_deleted(limit, offset)

    def count_deleted(self) -> int:
        return self._trash.count_deleted()

    def get_deleted(self, asset_id: int) -> Optional[TrashedAsset]:
        return self._trash.get_deleted(asset_id)

    def get_state(self) -> TrashState:
        """暴露单例水位状态，供 UI / 运维展示。"""
        return self._state.get()

    # ────────────────────────────────────────────────────────────────
    # 恢复
    # ────────────────────────────────────────────────────────────────
    def restore(self, asset_ids: List[int]) -> RestoreReport:
        """批量恢复。

        策略
        ----
        1. 先尝试整批恢复（快路径）。
        2. 整批因唯一索引冲突失败时回滚 → 逐条重试，收集冲突项。
           SQLite 的 ``IntegrityError`` 会让整批回滚，所以逐条重试是
           安全的——所有 id 都还在垃圾箱里。

        冲突原因当前只有一种：``idx_assets_source_file_path_active``
        上的 ``(source_id, file_path)`` 与现有 active 资产重复。
        """
        if not asset_ids:
            return RestoreReport(restored=[], not_found=[], conflicts=[])

        try:
            restored = self._trash.restore_batch(asset_ids)
        except sqlite3.IntegrityError:
            logger.warning(
                "Batch restore conflict, falling back to per-id retry "
                "(ids=%d)", len(asset_ids),
            )
            return self._restore_one_by_one(asset_ids)

        restored_set = set(restored)
        not_found = [aid for aid in asset_ids if aid not in restored_set]
        return RestoreReport(
            restored=restored,
            not_found=not_found,
            conflicts=[],
        )

    def _restore_one_by_one(self, asset_ids: List[int]) -> RestoreReport:
        restored: List[int] = []
        not_found: List[int] = []
        conflicts: List[RestoreConflict] = []

        for aid in asset_ids:
            try:
                result = self._trash.restore_batch([aid])
            except sqlite3.IntegrityError:
                # 事务已回滚，资产仍在垃圾箱，可以查到 file_path
                asset = self._trash.get_deleted(aid)
                file_path = asset.file_path if asset is not None else "<unknown>"
                conflicts.append(RestoreConflict(
                    asset_id=aid,
                    file_path=file_path,
                    reason="path_conflict",
                ))
                continue

            if result:
                restored.append(aid)
            else:
                not_found.append(aid)

        return RestoreReport(
            restored=restored,
            not_found=not_found,
            conflicts=conflicts,
        )

    # ────────────────────────────────────────────────────────────────
    # 恢复（uuid 面）
    # ────────────────────────────────────────────────────────────────
    def restore_by_uuid(self, asset_uuid: Union[UUID, str]) -> RestoreResult:
        """按 uuid 恢复单条资产（A4 / 原 G-R3）。

        做什么：等价于 ``restore_by_uuids([asset_uuid])``——uuid→id 桥接
        在**本层**完成，数字 id 不出 ``services/trash/``。
        返回：``RestoreResult``（``restored`` / ``not_found`` /
        ``conflicts``，全部是 uuid 面）。
        抛：与 :meth:`restore` 一致（底层 sqlite 异常穿透）。
        前置条件：资产应在回收站；不在 → 出现在 ``not_found``。
        原子性：单条；与 ``restore`` 同——整批快路径失败时降级为逐条重试，
        **不整批回滚**（一条冲突不影响其它条）。

        关系：新包装（非替代），``restore(List[int])`` 行为不变。
        """
        return self.restore_by_uuids([asset_uuid])

    def restore_by_uuids(
            self, asset_uuids: Sequence[Union[UUID, str]]
    ) -> RestoreResult:
        """按 uuid 批量恢复，返回 uuid 面结果（A4 / 原 G-R3）。

        做什么：

        1. ``TrashRepository.list_deleted_by_uuids`` 一次批量把 uuid 映射到
           回收站行（拿内部 ``id``；映射只在本层存活）；
        2. 复用既有 :meth:`restore` 的"整批快路径 + 唯一索引冲突降级逐条
           重试"策略，不复制第二套恢复逻辑；
        3. 把 id 面 ``RestoreReport`` 翻译回 uuid 面 ``RestoreResult``
           （冲突原因 ``"path_conflict"`` → ``RestoreConflictReason``）。

        返回：``RestoreResult``。``not_found`` = 入参里不在回收站的行（按
        输入顺序在前）+ 并发下快路径确认不到的行；``conflicts`` 的
        ``detail`` 是底层 ``file_path``（仅日志用）。
        抛：与 :meth:`restore` 一致。
        前置条件：无。
        原子性：与 :meth:`restore` 一致——逐条处理冲突，**不整批回滚**。

        关系：新语义接口，``id`` 面 :meth:`restore` 保留给既有调用点。
        """
        uuids: list[UUID] = []
        seen: set[UUID] = set()
        for value in asset_uuids:
            try:
                parsed = to_uuid(value)
            except (ValueError, AttributeError, TypeError):
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            uuids.append(parsed)
        if not uuids:
            return RestoreResult()

        rows = self._trash.list_deleted_by_uuids([str(u) for u in uuids])
        by_uuid = {row.uuid: row for row in rows}
        in_trash = [u for u in uuids if u in by_uuid]
        not_found = [u for u in uuids if u not in by_uuid]
        if not in_trash:
            return RestoreResult(not_found=tuple(not_found))

        report = self.restore([by_uuid[u].id for u in in_trash])
        id_to_uuid = {by_uuid[u].id: u for u in in_trash}

        restored = tuple(
            id_to_uuid[aid] for aid in report.restored if aid in id_to_uuid
        )
        # 快路径后仍"没恢复"的 id：并发下已被恢复 / 行已消失，一律算 not_found。
        vanished = tuple(
            id_to_uuid[aid] for aid in report.not_found if aid in id_to_uuid
        )
        conflicts = tuple(
            UuidRestoreConflict(
                asset_uuid=id_to_uuid[c.asset_id],
                reason=_to_conflict_reason(c.reason),
                detail=c.file_path,
            )
            for c in report.conflicts
            if c.asset_id in id_to_uuid
        )
        return RestoreResult(
            restored=restored,
            not_found=(*not_found, *vanished),
            conflicts=conflicts,
        )

    # ────────────────────────────────────────────────────────────────
    # 定时清理（调度器周期调用，例如每小时一次）
    # ────────────────────────────────────────────────────────────────
    def cleanup_expired(self) -> CleanupReport:
        """按保留期物理清除过期垃圾箱资产。

        返回本轮完整结果：删除列表 + 时钟诊断 + 更新后的水位状态。
        """
        now = self._clock()
        old_state = self._state.get()
        watermark = parse_iso(old_state.last_observed_at)

        kind, cutoff_iso, new_watermark = self._resolve_cleanup_window(
            now, watermark,
        )

        purged: List[UUID] = []
        thumb_failures: List[ThumbFailure] = []

        if cutoff_iso is not None:
            report = self._purge_loop(
                fetch_page=partial(
                    self._trash.find_expired,
                    cutoff_iso
                ),
            )
            # PurgeReport 的集合字段是 tuple（不可变）；CleanupReport 是
            # 既有 list 面接口，这里显式转换，不改既有 DTO 字段类型。
            purged = list(report.purged)
            thumb_failures = list(report.thumb_failures)
            if not report.completed:
                # 清理循环中途失败：本轮结果不完整，但已删清单要如实上报。
                logger.warning(
                    "Trash cleanup ended prematurely: purged=%d error=%r",
                    len(purged), report.error,
                )

        self._state.update(
            last_observed_at=format_iso(new_watermark),
            last_cleanup_at=format_iso(now),
            cleaned=len(purged),
            clock_jumps=0 if kind == "normal" else 1,
        )

        if kind == "normal":
            logger.info(
                "Trash cleanup done: purged=%d cutoff=%s watermark=%s",
                len(purged), cutoff_iso, format_iso(new_watermark),
            )
        else:
            logger.warning(
                "Trash cleanup skipped (%s): now=%s watermark=%s "
                "new_watermark=%s",
                kind, format_iso(now), format_iso(watermark),
                format_iso(new_watermark),
            )
        if thumb_failures:
            logger.warning(
                "Trash cleanup: %d thumb deletion failure(s)",
                len(thumb_failures),
            )

        return CleanupReport(
            purged=purged,
            clock_jump=ClockJump(kind=kind, advised_cutoff=cutoff_iso),
            state=self._state.get(),
        )

    # ────────────────────────────────────────────────────────────────
    # 用户手动清空垃圾箱
    # ────────────────────────────────────────────────────────────────
    def empty_trash(self) -> PurgeReport:
        """用户点击"清空垃圾箱"时调用。

        与 ``cleanup_expired`` 的差别：
        * 不判断保留期，不理会水位线——这是显式的用户意图。
        * 每批独立短事务，避免清空大库时长时间锁库。
        * 返回物理删除列表；缩略图删除失败信息一并返回但不阻塞流程。
        """
        logger.info("Emptying trash (user-initiated)")
        report = self._purge_loop(
            fetch_page=lambda n: self._trash.find_any_deleted(n),
        )
        logger.info(
            "Trash emptied: purged=%d thumb_failures=%d",
            len(report.purged), len(report.thumb_failures),
        )
        return report

    # ────────────────────────────────────────────────────────────────
    # 内部：时钟判定
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_cleanup_window(
            now: datetime,
            watermark: datetime,
    ) -> tuple[str, Optional[str], datetime]:
        """返回 ``(kind, cutoff_iso, new_watermark)``。

        * ``kind``：``"normal"`` / ``"backward"`` / ``"forward"``。
        * ``cutoff_iso``：本轮允许使用的截止时间；``None`` 表示本轮跳过清理。
        * ``new_watermark``：本轮结束后要写入的水位，单调不减。
        """
        if now < watermark:
            # 时钟回拨：用旧水位当"现在"，水位冻结。
            # 用户把时间调到过去时，垃圾箱不会提前清理。
            return "backward", None, watermark

        delta = now - watermark
        if delta > FORWARD_SKEW:
            # 时钟前跳：本轮不清理，水位最多推进 ADVANCE_PER_CYCLE。
            # 用户拨表一次不会触发大批误删；下一轮若 now 回到水位附近，
            # 自动恢复 normal。
            new_wm = min(now, watermark + ADVANCE_PER_CYCLE)
            return "forward", None, new_wm

        cutoff_iso = format_iso(now - TRASH_GRACE)
        return "normal", cutoff_iso, now

    # ────────────────────────────────────────────────────────────────
    # 内部：清理循环
    # ────────────────────────────────────────────────────────────────
    def _purge_loop(
            self,
            fetch_page: Callable[[int], List[PurgeCandidate]],
    ) -> PurgeReport:
        """分页循环：取一批 → 物理删除 → 删缩略图。返回七字段的
        ``PurgeReport``（``empty_trash`` 与 ``cleanup_expired`` 共用）。

        做什么
        ------
        1. 收工前计数快照 ``requested``；
        2. 逐批 ``fetch_page`` → ``purge_batch`` → 删缩略图；
        3. 每批把"实际删除"记进 ``purged``、把"曾是候选但没删掉"（并发
           恢复 / 状态已变）记进 ``skipped``——**两者不重叠**；
        4. 收工后再查一次 ``count_deleted`` 作为 ``count_after``。

        分页安全性：``find_*`` 按 ``deleted_at ASC, id ASC`` 排序，
        每批删除后下一批自然成为新的头部，无需 offset。若单批返回数
        小于 ``PURGE_BATCH_SIZE``，说明已扫到底，退出。

        异常语义（**什么算致命**）
        -------------------------
        * **致命**（直接穿透，报告不返回）：``sqlite3.ProgrammingError``
          （API 误用 / 连接已关闭，继续循环没有意义）、``MemoryError``；
        * **批级**（记录下来继续收尾）：其它所有 ``Exception``——典型是
          ``OperationalError``（连接被独占、磁盘错误）与 ``IntegrityError``。
          此时 **停止循环**，``completed=False``、``error`` 给出异常对象，
          **已完成部分的 ``purged`` 原样保留**（不得因异常吞掉已删清单）。
        * ``BaseException``（``KeyboardInterrupt`` / ``SystemExit``）不捕获。

        收工计数 ``count_deleted`` 自身失败时退化为 ``requested``：计数失败
        不该抹掉"已经删掉了哪些"。

        关系：``empty_trash``（用户显式清空）与 ``cleanup_expired``（保留期
        到期）共用本循环，唯一差别是 ``fetch_page`` 的取数条件。
        """
        requested = self.count_deleted()
        purged_all: List[UUID] = []
        skipped_all: List[UUID] = []
        failures: List[ThumbFailure] = []
        error: Optional[BaseException] = None
        completed = True

        while True:
            try:
                batch = fetch_page(PURGE_BATCH_SIZE)
                if not batch:
                    break

                ids = [c.id for c in batch]
                deleted = self._trash.purge_batch(ids)
                deleted_set = set(deleted)

                # 实际删除 → purged；候选但没删掉 → skipped（两列互斥）。
                for cand in batch:
                    if cand.id in deleted_set:
                        purged_all.append(cand.uuid)
                    else:
                        skipped_all.append(cand.uuid)

                # 仅对"实际被删除"的候选行删缩略图：并发恢复可能让某几行
                # 逃过 purge_batch 的二次校验，其文件必须保留。
                for cand in batch:
                    if cand.id not in deleted_set:
                        continue
                    err = self._delete_thumb_files(cand)
                    if err is not None:
                        failures.append(ThumbFailure(
                            asset_id=cand.id, error=err,
                        ))

                # 乐观推进：本批不足配额 → 已到底
                if len(batch) < PURGE_BATCH_SIZE:
                    break
            except _FATAL_PURGE_ERRORS:
                raise
            except Exception as exc:  # 批级异常转字段（见 docstring 异常语义）
                logger.exception(
                    "Purge loop stopped on batch error "
                    "(purged=%d, skipped=%d)", len(purged_all),
                    len(skipped_all),
                )
                completed = False
                error = exc
                break

        try:
            count_after = self.count_deleted()
        except Exception:  # 计数失败不该抹掉已删清单
            count_after = requested

        return PurgeReport(
            purged=tuple(purged_all),
            thumb_failures=tuple(failures),
            requested=requested,
            skipped=tuple(skipped_all),
            completed=completed,
            error=error,
            count_after=count_after,
        )

    # ────────────────────────────────────────────────────────────────
    # 内部：文件清理
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _delete_thumb_files(cand: PurgeCandidate) -> Optional[str]:
        """删除候选行的缩略图目录。

        返回 ``None`` 表示成功，否则返回错误摘要字符串。

        语义：``thumb_path`` 是**绝对目录**（``thumb_root/uuid/version``），
        其下所有 spec 文件属于该资产。直接 ``rmtree`` 即可，无需逐个处理
        ``thumb_small_path`` 等相对字段。

        ``thumb_path`` 为空表示从未生成过缩略图，跳过。
        """
        thumb_path = cand.thumb_path
        if not thumb_path:
            return None

        path = Path(thumb_path)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as e:
            return f"{path}: {e}"
        return None
