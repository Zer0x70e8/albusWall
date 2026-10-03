#
""""""

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
    CandidateStatus.PENDING: frozenset({CandidateStatus.PROCESSING}),
    # processing 允许回到 pending：recover_stale_candidates 会把超时任务重置
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
