#
"""资产写操作 —— 事件与结果 DTO（``*Result`` / ``*Report``）。

归属（``dto/CONVENTIONS.md`` · DTO 归属与命名）：事件与结果类。由
``AssetOpsService`` 产出、供 presenter / 撤销栈 / 二次确认文案消费，
既不是 repo 的浏览视图，也不是 repo 的写模型。

设计原则
--------
* **逐项区分** ``updated / noop / not_found / skipped(原因)``，不用单一
  ``rowcount`` / ``bool`` 冒充结果——仓储的一行 UPDATE 返回 0，
  既可能是"已存在"，也可能是"并发下状态变了"，两者语义不同。
* ``skipped`` 携带 :class:`SkipReason`（领域枚举），**不承载 UI 文案**。
* 所有标识字段只暴露资产 / 相册 **uuid**；数字 id 仅出现在明确标注为
  "底层透传"的字段上（见 :class:`PurgeReport.thumb_failures`）。
* 全部 ``frozen=True, slots=True`` 不可变；集合字段用 ``tuple``
  （可哈希、不可被调用方原地改）。

与 ``dto.trash`` 的分工
-----------------------
============================  ===========================================
本模块（对外，uuid 为键）      ``dto.trash``（TrashService 内部，id 为键）
============================  ===========================================
``PurgeReport``               ``PurgeReport``（**同一个类**；D3：由本模块
                              定义，``dto.trash`` 再导出）
``RestoreResult``             ``RestoreReport``（``restored: List[int]``）
``RestoreConflict``           ``RestoreConflict``（``asset_id`` + ``file_path``）
============================  ===========================================

``PurgeReport`` 已收敛为**单一版本**（D3）：物理清除在 ``TrashService``
内部就拿到 ``uuid``（``PurgeCandidate.uuid``），因此该报告天然是 uuid 面，
不再有 id 面旧版要翻译。``restore`` 仍是 id 面，由 ``TrashService`` 在
``services/trash/`` 内部桥接成 :class:`RestoreResult`。

本模块从 ``dto.trash`` 只 import ``ThumbFailure``（透传类型）；``dto.trash``
对本模块的 ``PurgeReport`` 再导出是**惰性**的（模块级 ``__getattr__``），
避免 ``dto.trash ↔ dto.asset.events`` 的循环 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID

from albuswall.dto.trash import ThumbFailure

__all__ = [
    "ClassifiedAssets",
    "FavoriteResult",
    "MembershipResult",
    "MoveResult",
    "PurgeReport",
    "RestoreConflict",
    "RestoreConflictReason",
    "RestoreResult",
    "SkipReason",
    "SkippedAsset",
    "TrashResult",
]


# ──────────────────────────────────────────────────────────────────────
# 跳过原因（领域枚举，不是 UI 文案）
# ──────────────────────────────────────────────────────────────────────
class SkipReason(str, Enum):
    """批量写操作中单个资产被跳过的原因。

    继承 ``str`` 便于日志 / IPC 序列化，但**不要**直接把 ``.value``
    或 ``.name`` 塞给用户看——文案归 presenter。
    """

    ASSET_MISSING = "asset_missing"
    """资产 uuid 不存在。"""

    ASSET_NOT_VISIBLE = "asset_not_visible"
    """资产已软删 / 所属导入源已软删。"""

    TARGET_ALBUM_NOT_VISIBLE = "target_album_not_visible"
    """目标相册已被软删（存在但不可作为写入目标）。"""

    SOURCE_ALBUM_NOT_VISIBLE = "source_album_not_visible"
    """源相册已被软删（``move_between_albums`` 用）。"""

    ASSET_NOT_IN_SOURCE = "asset_not_in_source"
    """资产可见，但本来就不在源相册里（``move_between_albums`` 用）。

    初稿未列此原因，但移动语义必须区分"在源相册待搬"与"根本不在源相册"，
    否则 ``moved`` 会虚高、``already_in_target`` 会漏报。
    """

    UNKNOWN = "unknown"
    """仓储给出了本层不认识的跳过原因（词表漂移的兜底）。

    出现即说明底层 ``MoveSkipReason`` 新增了取值而映射表没跟上——
    结果仍可用，但应当补映射，而不是把未知原因当成"已处理"。
    """


@dataclass(frozen=True, slots=True)
class SkippedAsset:
    """一条被跳过的资产及其原因。"""

    asset_uuid: UUID
    reason: SkipReason


# ──────────────────────────────────────────────────────────────────────
# 收藏
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FavoriteResult:
    """``AssetOpsService.set_favorite`` 的结果。

    ``outcome``：

    * ``"updated"``  —— 值确实被改写；
    * ``"noop"``     —— 旧值已等于目标值，**未写库**；
    * ``"not_found"``—— 资产 uuid 不存在。

    ``previous_value`` 在 ``updated`` / ``noop`` 时有值（前者供撤销记录，
    后者供调用方确认"本来就是这个值"）。
    """

    asset_uuid: UUID
    outcome: Literal["updated", "noop", "not_found"]
    previous_value: bool | None = None


# ──────────────────────────────────────────────────────────────────────
# 回收站
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TrashResult:
    """``trash`` 的结果。三列**互斥**，``changed`` 只含真正被软删的资产。

    撤销栈只能取 ``changed``——``already_deleted`` / ``missing`` 上没有
    发生状态变化，记账会把撤销方向搞反。
    """

    changed: tuple[UUID, ...] = ()
    already_deleted: tuple[UUID, ...] = ()
    missing: tuple[UUID, ...] = ()


class RestoreConflictReason(str, Enum):
    """单条恢复失败的原因（领域枚举）。

    ``PATH_CONFLICT`` 对应底层唯一索引
    ``idx_assets_source_file_path_active`` 上的 ``(source_id, file_path)``
    冲突——这也是 ``TrashService`` 目前唯一会产生冲突的原因
    （底层原始字符串为 ``"path_conflict"``）。
    """

    PATH_CONFLICT = "path_conflict"
    UNIQUE_CONSTRAINT = "unique_constraint"
    INVALID_STATE = "invalid_state"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RestoreConflict:
    """单条恢复失败的原因。

    ``detail`` 是底层给的补充信息（例如冲突的 ``file_path``），
    仅用于日志，**不用于展示**。
    """

    asset_uuid: UUID
    reason: RestoreConflictReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """``restore`` 的结果。逐条处理，一条冲突不影响其他条。"""

    restored: tuple[UUID, ...] = ()
    not_found: tuple[UUID, ...] = ()
    conflicts: tuple[RestoreConflict, ...] = ()


# ──────────────────────────────────────────────────────────────────────
# 相册成员关系
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MembershipResult:
    """``add_to_album`` / ``remove_from_album`` / ``copy_into_album`` 共用结果。

    * ``add_to_album`` / ``copy_into_album`` 使用 ``added / existing / skipped``，
      ``removed`` / ``not_present`` 为空；
    * ``remove_from_album`` 使用 ``removed / not_present / skipped``，
      ``added`` / ``existing`` 为空。

    两组字段共存而不是拆两个 DTO：调用方（撤销栈 / 目标选择器）需要
    统一形状，且两组的互斥性由方法语义保证，不是靠类型保证。
    """

    added: tuple[UUID, ...] = ()
    existing: tuple[UUID, ...] = ()
    removed: tuple[UUID, ...] = ()
    not_present: tuple[UUID, ...] = ()
    skipped: tuple[SkippedAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class MoveResult:
    """``move_between_albums`` 的结果。

    原子操作：要么全部生效，要么抛
    :class:`~albuswall.services.errors.BatchAtomicityError` 全回滚。

    因此本 DTO **没有** ``compensated`` / ``orphan_in_target`` 这类
    "补偿式方案"才需要的字段——真原子实现下它们恒为空，留着只会
    让调用方以为"可能存在部分生效"。
    """

    moved: tuple[UUID, ...] = ()
    already_in_target: tuple[UUID, ...] = ()
    skipped: tuple[SkippedAsset, ...] = ()


# ──────────────────────────────────────────────────────────────────────
# 清空回收站
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PurgeReport:
    """``empty_trash`` / ``cleanup_expired`` 的结果（**唯一版本**，uuid 面）。

    本类是 ``PurgeReport`` 的全项目唯一定义：``dto/trash.py`` 按 D3 从
    本模块再导出，``TrashService`` 直接返回它，``AssetOpsService`` 透传
    给 UI。字段语义：

    * ``purged``         —— 实际被物理删除的资产 uuid。
    * ``thumb_failures`` —— 缩略图删除失败记录，**底层透传**，非本服务产生。
      其中 ``ThumbFailure.asset_id`` 是 ``assets.id``（``dto.trash`` 既有
      契约，``TrashService`` 在 DB 行删除后才回报，此时行已不存在，
      无法据此反查 uuid），因此这是本模块唯一允许出现的数字 id；
      调用方不得把它用于任何 uuid 寻址。
    * ``requested``      —— 执行**前**的回收站计数快照（``count_before``
      别名保留兼容）。
    * ``skipped``        —— 曾是清理候选、最终**没被删掉**的资产 uuid
      （并发恢复 / 快照之间状态变化 / 异常中断）。不变量：
      ``purged`` 与 ``skipped`` **不重叠**，UI 的 ``len(purged)`` 文案
      依赖这一点。
    * ``completed``      —— 批级流程是否完整跑完；``False`` 表示中途异常
      停止，此时 ``purged`` 仍保留**已完成部分**，``error`` 给出原因。
    * ``error``          —— 导致未完成（或部分完成）的异常对象。
    * ``count_after``    —— 执行**后**的回收站计数快照。
    """

    purged: tuple[UUID, ...] = ()
    thumb_failures: tuple[ThumbFailure, ...] = ()
    requested: int = 0
    skipped: tuple[UUID, ...] = ()
    completed: bool = True
    error: BaseException | None = None
    count_after: int = 0

    @property
    def partial(self) -> bool:
        """是否"部分完成"（``completed`` 的反面，语义更贴近调用方措辞）。"""
        return not self.completed

    @property
    def count_before(self) -> int:
        """``requested`` 的别名：初稿字段名，保留兼容。"""
        return self.requested

    @property
    def skipped_count(self) -> int:
        """``skipped`` 的条数（旧 ``skipped: int`` 语义的读取方式）。"""
        return len(self.skipped)


# ──────────────────────────────────────────────────────────────────────
# 分类快照
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ClassifiedAssets:
    """一次批量查询出的资产三分类。三类**互斥**，并集 = 去重后的输入。"""

    visible: tuple[UUID, ...] = ()
    deleted: tuple[UUID, ...] = ()
    missing: tuple[UUID, ...] = ()
