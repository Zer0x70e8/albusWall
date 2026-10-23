#
"""source 域枚举：导入源候选状态。"""

from __future__ import annotations

from enum import Enum


class CandidateStatus(str, Enum):
    """asset_candidate_cache.status 的合法取值。

    必须与 resources/sql/media_library_schema.sql 里的
    CONSTRAINT chk_candidate_status 严格一致。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    def can_transition_to(self, target: "CandidateStatus") -> bool:
        return target in _ALLOWED_TRANSITIONS[self]


# ---- 状态机：放模块级，避免被 EnumMeta 收成成员 ----

_TERMINAL: frozenset[CandidateStatus] = frozenset({
    CandidateStatus.DONE,
    CandidateStatus.SKIPPED,
    CandidateStatus.FAILED,
})

_ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    # pending 允许直接到 skipped / failed：当前 ImportService 取的是 pending
    # 候选，``_process_batch`` **不** claim（全程状态仍是 pending），所以
    # skip / fail 都是 pending → 终态的直连转移。
    #   * PENDING → SKIPPED：未领取就被判"跳过"（path_invalid / magic_unknown）；
    #   * PENDING → FAILED ：未领取就判"失败"（文件消失 / worker 异常）。
    #     **改前缺失**：fail 候选永远停在 pending，被下一轮反复捞出重试，
    #     ValueError 只在日志里可见（既存 bug，本任务修掉）。
    # 注：PENDING → DONE 不在此列——finalize_candidate 直接 UPDATE 落 DONE，
    # 不走状态机；保持允许集最小，避免掩盖"未领取就完成"的异常路径。
    CandidateStatus.PENDING: frozenset({
        CandidateStatus.PROCESSING,
        CandidateStatus.SKIPPED,
        CandidateStatus.FAILED,
    }),
    # processing 允许回到 pending：recover_stale_candidates 会把超时任务重置
    # processing → skipped / failed：为将来补 claim 流程预留（当前链路用不上）。
    CandidateStatus.PROCESSING: frozenset({
        CandidateStatus.DONE,
        CandidateStatus.SKIPPED,
        CandidateStatus.FAILED,
        CandidateStatus.PENDING,
    }),
    CandidateStatus.DONE: frozenset(),
    CandidateStatus.SKIPPED: frozenset(),
    CandidateStatus.FAILED: frozenset(),
}
