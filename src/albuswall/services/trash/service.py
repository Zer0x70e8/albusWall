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
from typing import Callable, List, Optional

from albuswall.log import getLogger
from albuswall.dto.trash import (
    CleanupReportDTO,
    ClockJumpDTO,
    PurgeCandidateDTO,
    PurgeReportDTO,
    RestoreConflictDTO,
    RestoreReportDTO,
    TrashedAssetDTO,
    ThumbFailureDTO,
)
from albuswall.repositories.trash import (
    TrashRepository,
    TrashStateRepository,
)

logger = getLogger(__name__)

# ── 策略常量 ────────────────────────────────────────────────────────
TRASH_GRACE = timedelta(days=30)
"""软删除资产的保留期。超过后由 ``cleanup_expired`` 物理清除。"""

FORWARD_SKEW = timedelta(hours=6)
"""判定"时钟前跳"的阈值：``now - watermark`` 超过它即视为可疑。"""

ADVANCE_PER_CYCLE = timedelta(hours=1)
"""检测到前跳后，水位每轮最多推进的时长。"""

PURGE_BATCH_SIZE = 500
"""清理循环的单批大小。过大 → 单事务锁库过久；过小 → 循环开销显著。"""


# ── 默认时钟实现 ────────────────────────────────────────────────────
def utcnow() -> datetime:
    """默认时钟：返回带 UTC tz 的当前时间。

    生产代码通过容器把它注册为 ``"clock"``；测试可注入任意
    ``Callable[[], datetime]``。公共名字（无下划线）：它是可复用的
    默认实现，不是模块私有细节。
    """
    return datetime.now(timezone.utc)


def _format_iso(dt: datetime) -> str:
    """与 SQLite ``strftime('%Y-%m-%dT%H:%M:%f','now')`` 同构：UTC + 毫秒。

    ``%f`` 输出 6 位微秒，截断到 3 位毫秒，与 SQLite 一致。
    在该格式下字符串字典序等于时间序，``deleted_at <= ?`` 成立。
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


# noinspection SpellCheckingInspection
def _parse_iso(raw: str) -> datetime:
    """容错解析：兼容带/不带 ``Z``、带/不带 tzinfo。

    schema 生成的时间串不带 tz，视为 UTC。
    """
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
    def list_deleted(self, limit: int, offset: int) -> List[TrashedAssetDTO]:
        return self._trash.list_deleted(limit, offset)

    def count_deleted(self) -> int:
        return self._trash.count_deleted()

    def get_deleted(self, asset_id: int) -> Optional[TrashedAssetDTO]:
        return self._trash.get_deleted(asset_id)

    def get_state(self):
        """暴露单例水位状态，供 UI / 运维展示。"""
        return self._state.get()

    # ────────────────────────────────────────────────────────────────
    # 恢复
    # ────────────────────────────────────────────────────────────────
    def restore(self, asset_ids: List[int]) -> RestoreReportDTO:
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
            return RestoreReportDTO(restored=[], not_found=[], conflicts=[])

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
        return RestoreReportDTO(
            restored=restored,
            not_found=not_found,
            conflicts=[],
        )

    def _restore_one_by_one(self, asset_ids: List[int]) -> RestoreReportDTO:
        restored: List[int] = []
        not_found: List[int] = []
        conflicts: List[RestoreConflictDTO] = []

        for aid in asset_ids:
            try:
                result = self._trash.restore_batch([aid])
            except sqlite3.IntegrityError:
                # 事务已回滚，资产仍在垃圾箱，可以查到 file_path
                asset = self._trash.get_deleted(aid)
                file_path = asset["file_path"] if asset is not None else "<unknown>"
                conflicts.append(RestoreConflictDTO(
                    asset_id=aid,
                    file_path=file_path,
                    reason="path_conflict",
                ))
                continue

            if result:
                restored.append(aid)
            else:
                not_found.append(aid)

        return RestoreReportDTO(
            restored=restored,
            not_found=not_found,
            conflicts=conflicts,
        )

    # ────────────────────────────────────────────────────────────────
    # 定时清理（调度器周期调用，例如每小时一次）
    # ────────────────────────────────────────────────────────────────
    def cleanup_expired(self) -> CleanupReportDTO:
        """按保留期物理清除过期垃圾箱资产。

        返回本轮完整结果：删除列表 + 时钟诊断 + 更新后的水位状态。
        """
        now = self._clock()
        old_state = self._state.get()
        watermark = _parse_iso(old_state["last_observed_at"])

        kind, cutoff_iso, new_watermark = self._resolve_cleanup_window(
            now, watermark,
        )

        purged: List[int] = []
        thumb_failures: List[ThumbFailureDTO] = []

        if cutoff_iso is not None:
            report = self._purge_loop(
                fetch_page=partial(
                    self._trash.find_expired,
                    cutoff_iso
                ),
            )
            purged = report["purged"]
            thumb_failures = report["thumb_failures"]

        self._state.update(
            last_observed_at=_format_iso(new_watermark),
            last_cleanup_at=_format_iso(now),
            cleaned=len(purged),
            clock_jumps=0 if kind == "normal" else 1,
        )

        if kind == "normal":
            logger.info(
                "Trash cleanup done: purged=%d cutoff=%s watermark=%s",
                len(purged), cutoff_iso, _format_iso(new_watermark),
            )
        else:
            logger.warning(
                "Trash cleanup skipped (%s): now=%s watermark=%s "
                "new_watermark=%s",
                kind, _format_iso(now), _format_iso(watermark),
                _format_iso(new_watermark),
            )
        if thumb_failures:
            logger.warning(
                "Trash cleanup: %d thumb deletion failure(s)",
                len(thumb_failures),
            )

        return CleanupReportDTO(
            purged=purged,
            clock_jump=ClockJumpDTO(kind=kind, advised_cutoff=cutoff_iso),
            state=self._state.get(),
        )

    # ────────────────────────────────────────────────────────────────
    # 用户手动清空垃圾箱
    # ────────────────────────────────────────────────────────────────
    def empty_trash(self) -> PurgeReportDTO:
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
            len(report["purged"]), len(report["thumb_failures"]),
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

        cutoff_iso = _format_iso(now - TRASH_GRACE)
        return "normal", cutoff_iso, now

    # ────────────────────────────────────────────────────────────────
    # 内部：清理循环
    # ────────────────────────────────────────────────────────────────
    def _purge_loop(
            self,
            fetch_page: Callable[[int], List[PurgeCandidateDTO]],
    ) -> PurgeReportDTO:
        """分页循环：取一批 → 物理删除 → 删缩略图。

        分页安全性：``find_*`` 按 ``deleted_at ASC, id ASC`` 排序，
        每批删除后下一批自然成为新的头部，无需 offset。若单批返回数
        小于 ``PURGE_BATCH_SIZE``，说明已扫到底，退出。
        """
        purged_all: List[int] = []
        failures: List[ThumbFailureDTO] = []

        while True:
            batch = fetch_page(PURGE_BATCH_SIZE)
            if not batch:
                break

            ids = [c["id"] for c in batch]
            deleted = self._trash.purge_batch(ids)
            deleted_set = set(deleted)
            purged_all.extend(deleted)

            # 仅对"实际被删除"的候选行删缩略图：并发恢复可能让某几行
            # 逃过 purge_batch 的二次校验，其文件必须保留。
            for cand in batch:
                if cand["id"] not in deleted_set:
                    continue
                err = self._delete_thumb_files(cand)
                if err is not None:
                    failures.append(ThumbFailureDTO(
                        asset_id=cand["id"], error=err,
                    ))

            # 乐观推进：本批不足配额 → 已到底
            if len(batch) < PURGE_BATCH_SIZE:
                break

        return PurgeReportDTO(purged=purged_all, thumb_failures=failures)

    # ────────────────────────────────────────────────────────────────
    # 内部：文件清理
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _delete_thumb_files(cand: PurgeCandidateDTO) -> Optional[str]:
        """删除候选行的缩略图目录。

        返回 ``None`` 表示成功，否则返回错误摘要字符串。

        语义：``thumb_path`` 是**绝对目录**（``thumb_root/uuid/version``），
        其下所有 spec 文件属于该资产。直接 ``rmtree`` 即可，无需逐个处理
        ``thumb_small_path`` 等相对字段。

        ``thumb_path`` 为空表示从未生成过缩略图，跳过。
        """
        thumb_path = cand["thumb_path"]
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
