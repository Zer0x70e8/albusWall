#
"""``AssetOpsService`` —— 资产写用例（收藏 / 回收站 / 相册归属 / 分类）。

职责边界
--------
**持有**：无运行时状态，不建连接、不读配置、不做 IO、不启动后台线程。

**依赖**（全部构造注入）：

* ``AssetRepository`` —— 写 ``assets``（收藏 / 软删）与 ``album_assets``（成员）；
* ``ViewRepository``  —— 可见性口径（"这条资产现在能不能被写"）。
  **只读**，与 ``ViewService`` 用的是同一个仓储、同一套 where 条件；
* ``TrashService``    —— 恢复 / 清空回收站（uuid 面）；
* ``AssetTagService`` —— 将来批量打标签时组合它，而不是抄一遍仓储调用
  （当前所有公开接口都不使用它，保留注入位是为了避免以后再改构造签名）。

与 ``ViewService`` 的分工：读走 view，写走这里。两者共用同一套可见性判定
（``ViewRepository``），所以"列表里看得到"和"写得动"永远一致。

写操作的记账纪律
----------------
所有批量接口一律以**仓储返回的实际变化**为准记账，不信输入：

* ``soft_delete_by_uuids`` 返回真正被软删的 uuid；
* ``add_to_album`` / ``remove_from_album`` 返回真正新增 / 删除的 uuid；
* ``move_between_albums`` 返回 repo 层的 ``MoveOutcome``。

原因是"先分类后写"两步之间必然有窗口（并发软删 / 恢复 / 用户在另一处操作），
而撤销栈只能记真正发生的事 —— 记多了会撤销出幻觉，记少了会留下脏数据。

不做线程切换：本服务只写 DB，不碰任何"属主线程状态"（对比源域的
``TrackingState``）。``Signal`` 也没有在这里发 —— 资产域现在没有订阅方；
等 UI 需要"资产变了"的事件时，按源域的做法加 ``SignalDescriptor`` 并在装配层
接线，不要在各方法里到处 emit。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID

from albuswall.dto.asset.events import (
    ClassifiedAssets,
    FavoriteResult,
    MembershipResult,
    MoveResult,
    PurgeReport,
    RestoreConflict,
    RestoreConflictReason,
    RestoreResult,
    SkipReason,
    SkippedAsset,
    TrashResult,
)
from albuswall.dto.utils import to_uuid
from albuswall.log import Logger, getLogger
from albuswall.service.errors import (
    AlbumNotFound,
    AssetNotFound,
    BatchAtomicityError,
    NotAMutableAlbum,
    NotAVisibleAsset,
)

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.repositories.asset import AssetRepository
    from albuswall.repositories.view import ViewRepository
    from albuswall.service.asset.trash import TrashService

__all__ = ["AssetOpsService"]

_UUIDLike = UUID | str

_logger: Logger = getLogger(__name__)

#: 仓储 ``classify_by_uuids`` 的状态词表 → 内部两态。
#: 仓储可能用 ``active`` / ``visible`` 两种写法（历史原因），两者都认。
_VISIBLE_STATES = frozenset({"visible", "active"})
_DELETED_STATES = frozenset({"deleted"})

#: repo 层 ``MoveSkipReason`` 字符串 → 领域 ``SkipReason``。
#: 两套词表是刻意的（仓储层不 import dto.asset）；这张表是唯一翻译点。
_MOVE_SKIP_REASON_MAP = {
    "asset_missing": SkipReason.ASSET_MISSING,
    "asset_invisible": SkipReason.ASSET_NOT_VISIBLE,
    "source_invisible": SkipReason.SOURCE_ALBUM_NOT_VISIBLE,
    "dst_album_missing": SkipReason.TARGET_ALBUM_NOT_VISIBLE,
    "dst_album_deleted": SkipReason.TARGET_ALBUM_NOT_VISIBLE,
    "not_in_source": SkipReason.ASSET_NOT_IN_SOURCE,
}

#: 恢复冲突原因 → 领域枚举（未知一律降级 ``UNKNOWN``，不让恢复整体失败）。
_CONFLICT_REASON_MAP = {
    "path_conflict": RestoreConflictReason.PATH_CONFLICT,
    "unique_constraint": RestoreConflictReason.UNIQUE_CONSTRAINT,
    "invalid_state": RestoreConflictReason.INVALID_STATE,
}


# ======================================================================
# 模块内工具
# ======================================================================
def _dedupe(uuids: Iterable[_UUIDLike]) -> tuple[UUID, ...]:
    """归一化为 ``UUID`` 并去重，保持首次出现顺序。

    去重放在**归一化之后**：``UUID`` 对象与同一个 uuid 的字符串输入会合并，
    否则同一资产会被写两次、在结果里出现两项。
    """
    return tuple(dict.fromkeys(to_uuid(u) for u in uuids))


def _as_uuid_set(values: Iterable[Any] | None) -> set[UUID]:
    """把仓储返回的 uuid 容器（UUID 或 str 混排）归一化为 set。"""
    if not values:
        return set()
    return {to_uuid(v) for v in values}


def _to_conflict_reason(raw: Any) -> RestoreConflictReason:
    """把底层冲突原因（枚举 / 字符串 / None）翻译成领域枚举（永不抛）。"""
    if isinstance(raw, RestoreConflictReason):
        return raw
    if raw is None:
        return RestoreConflictReason.UNKNOWN
    return _CONFLICT_REASON_MAP.get(str(raw), RestoreConflictReason.UNKNOWN)


def _conflict_uuid(conflict: Any) -> Any:
    """从底层 conflict 对象取 uuid（兼容 ``asset_uuid`` / ``uuid``）。

    不认 ``asset_id``：数字 id 在 uuid 面无法表达，静默转换会把"某条冲突"
    变成"另一条资产的冲突"，比失败危险。
    """
    for attr in ("asset_uuid", "uuid"):
        value = getattr(conflict, attr, None)
        if value is not None:
            return value
    raise NotImplementedError(
        "restore 冲突项既无 asset_uuid 也无 uuid：回收站层应返回 uuid 面结果"
        "（TrashService.restore_by_uuids）"
    )


class AssetOpsService:
    """资产写用例编排（被动服务）。"""

    def __init__(
            self,
            asset_repo: "AssetRepository",
            view_repo: "ViewRepository",
            trash_service: "TrashService",
            tag_service: Any = None,
    ) -> None:
        self._asset_repo = asset_repo
        self._view_repo = view_repo
        self._trash_service = trash_service
        # 预留：将来批量打标签时组合 AssetTagService，而不是抄它的仓储调用。
        self._tag_service = tag_service

    def describe(self, *, verbose: bool = False) -> str:
        base = "AssetOpsService()"
        if verbose:
            base = base[:-1] + (
                f"asset={type(self._asset_repo).__name__}, "
                f"view={type(self._view_repo).__name__}, "
                f"trash={type(self._trash_service).__name__})"
            )
        return base

    # ------------------------------------------------------------------ #
    # 守卫与分类
    # ------------------------------------------------------------------ #
    def classify_assets(
            self, asset_uuids: Iterable[_UUIDLike],
    ) -> ClassifiedAssets:
        """一次批量查出 visible / deleted / missing 三类资产。

        三类**互斥**，并集 = 去重后的输入（不额外扩展）。

        非原子：只是只读快照，不保证与后续写操作之间没有并发变化；写接口
        因此始终以仓储的"实际变化返回值"记账。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return ClassifiedAssets()

        rows = self._view_repo.classify_by_uuids(uuids) or {}
        # 契约键是 canonical str；这里再 to_uuid 归一化，容忍 UUID 键的实现，
        # 避免 dict.get(UUID) 静默 miss 把一切判成 missing。
        state_by_uuid = {to_uuid(k): v for k, v in dict(rows).items()}

        visible: list[UUID] = []
        deleted: list[UUID] = []
        missing: list[UUID] = []
        for u in uuids:
            state = state_by_uuid.get(u)
            if state is None:
                missing.append(u)
                continue
            normalized = str(state)
            if normalized in _VISIBLE_STATES:
                visible.append(u)
            elif normalized in _DELETED_STATES:
                deleted.append(u)
            else:
                # 未定义状态保守归为 missing：宁可跳过，不可误写。
                _logger.warning(
                    "unknown visibility state %r for asset %s; treat as missing",
                    state, u,
                )
                missing.append(u)

        return ClassifiedAssets(
            visible=tuple(visible),
            deleted=tuple(deleted),
            missing=tuple(missing),
        )

    def ensure_asset_visible(self, asset_uuid: _UUIDLike) -> None:
        """确保资产存在且可见。

        Raises:
            AssetNotFound: uuid 不存在。
            NotAVisibleAsset: 存在但不可见（已软删 / 源已下线），``reason``
                是底层状态字符串。
        """
        uuid = to_uuid(asset_uuid)
        state = (self._view_repo.classify_by_uuids([uuid]) or {}).get(str(uuid))
        if state is None:
            raise AssetNotFound(uuid)
        if str(state) not in _VISIBLE_STATES:
            raise NotAVisibleAsset(uuid, reason=str(state))

    def ensure_mutable_album(self, album_uuid: _UUIDLike) -> None:
        """确保相册存在且不是系统 / 回收站相册。

        顺序是刻意的：**先判回收站、再判系统相册、最后才判存在性**。
        虚拟相册（All / Trash）不落 ``albums`` 表，若先查存在性会把 Trash
        误判成 ``AlbumNotFound``，丢掉更具体的 ``reason="trash"``。

        Raises:
            NotAMutableAlbum: 回收站相册（``reason="trash"``）或其它系统相册
                （``reason="system"``）。
            AlbumNotFound: uuid 不对应任何相册行（含非法 uuid）。

        注意：本方法**不**检查普通相册是否可见（已软删）；那由各写接口转成
        逐项 ``skipped(TARGET_ALBUM_NOT_VISIBLE)``。
        """
        uuid = to_uuid(album_uuid)
        if self._view_repo.is_trash_album(uuid):
            raise NotAMutableAlbum(uuid, reason="trash")
        if self._view_repo.is_system_album(uuid):
            raise NotAMutableAlbum(uuid, reason="system")
        if not self._view_repo.album_exists(uuid):
            raise AlbumNotFound(uuid)

    def is_system_album(self, album_uuid: _UUIDLike) -> bool:
        """相册是否为系统相册（All / Trash 等虚拟相册）。"""
        return bool(self._view_repo.is_system_album(to_uuid(album_uuid)))

    def is_trash_album(self, album_uuid: _UUIDLike) -> bool:
        """相册是否为回收站相册。"""
        return bool(self._view_repo.is_trash_album(to_uuid(album_uuid)))

    # ------------------------------------------------------------------ #
    # 核心写操作
    # ------------------------------------------------------------------ #
    def set_favorite(
            self, asset_uuid: _UUIDLike, favorite: bool,
    ) -> FavoriteResult:
        """设置 / 取消资产收藏。

        读旧值 → 相同则**不写库** → 不同则写。返回的 ``outcome`` 区分
        ``updated``（``previous_value`` 供撤销）/ ``noop`` / ``not_found``。

        前置条件：无 —— 收藏对已软删资产同样合法（回收站里也能取消收藏），
        所以这里用 ``include_deleted=True`` 读。
        """
        uuid = to_uuid(asset_uuid)
        asset = self._view_repo.get_asset(uuid, include_deleted=True)
        if asset is None:
            return FavoriteResult(asset_uuid=uuid, outcome="not_found")

        current = bool(asset.is_favorite)
        if current == bool(favorite):
            return FavoriteResult(
                asset_uuid=uuid,
                outcome="noop",
                previous_value=current,
            )

        self._asset_repo.set_favorite(uuid, bool(favorite))
        return FavoriteResult(
            asset_uuid=uuid,
            outcome="updated",
            previous_value=current,
        )

    def trash(self, asset_uuids: Iterable[_UUIDLike]) -> TrashResult:
        """批量软删资产到回收站。

        三列**互斥**：

        * ``changed`` —— 真正被软删的 uuid（**撤销记录只能取这一列**）；
        * ``already_deleted`` —— 此前已在回收站的 uuid；
        * ``missing`` —— 不存在的 uuid。

        不因个别资产不可见而整批失败：trash 是"能删的就删"的语义。
        软删**不**清缩略图（回收站预览要用），文件与 DB 列的清理在
        ``TrashService`` 的保留期到期清理 / 清空回收站里发生。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return TrashResult()

        classified = self.classify_assets(uuids)
        changed: set[UUID] = set()
        if classified.visible:
            changed = _as_uuid_set(
                self._asset_repo.soft_delete_by_uuids(classified.visible)
            )

        return TrashResult(
            changed=tuple(u for u in classified.visible if u in changed),
            already_deleted=classified.deleted,
            missing=classified.missing,
        )

    def restore(self, asset_uuids: Iterable[_UUIDLike]) -> RestoreResult:
        """从回收站恢复一批资产。

        冲突（唯一索引 ``idx_assets_source_file_path_active``）走结果而不抛
        异常；**逐条处理，不整批回滚** —— 一条冲突不影响其它条恢复成功。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return RestoreResult()

        outcome = self._trash_service.restore_by_uuids(uuids)
        conflicts = tuple(
            RestoreConflict(
                asset_uuid=to_uuid(_conflict_uuid(c)),
                reason=_to_conflict_reason(getattr(c, "reason", None)),
                detail=getattr(c, "detail", None),
            )
            for c in (getattr(outcome, "conflicts", None) or ())
        )
        return RestoreResult(
            restored=_dedupe(getattr(outcome, "restored", None) or ()),
            not_found=_dedupe(getattr(outcome, "not_found", None) or ()),
            conflicts=conflicts,
        )

    def add_to_album(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Iterable[_UUIDLike],
    ) -> MembershipResult:
        """把一批资产加入目标相册（只加不删）。

        返回 ``added`` / ``existing`` / ``skipped``；``skipped`` 覆盖
        ``ASSET_MISSING`` / ``ASSET_NOT_VISIBLE`` / ``TARGET_ALBUM_NOT_VISIBLE``。

        Raises:
            AlbumNotFound: 目标相册不存在。
            NotAMutableAlbum: 目标相册是系统 / 回收站相册。

        目标相册**不可见**（已软删）时不抛，全部
        ``skipped(TARGET_ALBUM_NOT_VISIBLE)``。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MembershipResult()

        album = to_uuid(album_uuid)
        self.ensure_mutable_album(album)
        classified = self.classify_assets(uuids)

        if not self._album_is_visible(album):
            return MembershipResult(
                skipped=self._skip_all(
                    classified, SkipReason.TARGET_ALBUM_NOT_VISIBLE),
            )

        existing: set[UUID] = set()
        if classified.visible:
            existing = {
                uuid for uuid, is_member in self._asset_repo.memberships(
                    album, classified.visible,
                ).items() if is_member
            }

        to_add = [u for u in classified.visible if u not in existing]
        added: set[UUID] = set()
        if to_add:
            # 批量版：仓储返回"真正新增"的 uuid 集合（``rowcount`` 反推不出是
            # 哪一个）。单条 ``add_to_album`` 只服务"一条资产"的调用点。
            added = _as_uuid_set(self._asset_repo.add_many_to_album(album, to_add))

        return MembershipResult(
            added=tuple(u for u in to_add if u in added),
            existing=tuple(u for u in classified.visible if u in existing),
            skipped=self._asset_skips(classified),
        )

    def remove_from_album(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Iterable[_UUIDLike],
    ) -> MembershipResult:
        """把一批资产从目标相册移除（只删不加）。

        返回 ``removed`` / ``not_present`` / ``skipped``。

        **不要求资产当前可见** —— 允许把已软删资产从相册里清掉（回收站里的
        资产不该继续占着相册），所以存在性用 ``classify_assets`` 的
        ``visible + deleted`` 两桶之和，只有"根本不存在"才 skipped。

        Raises:
            AlbumNotFound / NotAMutableAlbum: 同 :meth:`add_to_album`。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MembershipResult()

        album = to_uuid(album_uuid)
        self.ensure_mutable_album(album)

        classified = self.classify_assets(uuids)
        if not self._album_is_visible(album):
            return MembershipResult(
                skipped=self._skip_all(
                    classified, SkipReason.TARGET_ALBUM_NOT_VISIBLE),
            )

        present = set(classified.visible) | set(classified.deleted)
        candidates = [u for u in uuids if u in present]
        missing = [u for u in uuids if u not in present]

        in_album: set[UUID] = set()
        if candidates:
            in_album = {
                uuid for uuid, is_member in self._asset_repo.memberships(
                    album, candidates,
                ).items() if is_member
            }

        to_remove = [u for u in candidates if u in in_album]
        not_present = [u for u in candidates if u not in in_album]

        removed: set[UUID] = set()
        if to_remove:
            removed = _as_uuid_set(
                self._asset_repo.remove_many_from_album(album, to_remove)
            )

        return MembershipResult(
            removed=tuple(u for u in to_remove if u in removed),
            not_present=tuple(not_present),
            skipped=tuple(
                SkippedAsset(u, SkipReason.ASSET_MISSING) for u in missing
            ),
        )

    def move_between_albums(
            self,
            src_album_uuid: _UUIDLike,
            dst_album_uuid: _UUIDLike,
            asset_uuids: Iterable[_UUIDLike],
    ) -> MoveResult:
        """把一批资产从源相册移动到目标相册（**真原子**）。

        服务层**不**循环调 add/remove，只调一次
        ``AssetRepository.move_between_albums`` —— 单事务，失败整体回滚。

        语义细节（与 repo 的 ``MoveOutcome`` 对齐）：

        * ``src == dst`` 的早退在**仓储内部**（不写任何行）；可见资产若不是
          该相册成员，归 ``skipped(ASSET_NOT_IN_SOURCE)``；
        * ``already_in_target`` 是 ``moved`` 的**子集**（既在源又在目标的资产
          只从源侧摘掉，不重复插入）。

        Raises:
            AlbumNotFound / NotAMutableAlbum: 源或目标相册的守卫失败。
            BatchAtomicityError: 仓储事务失败，**整批已回滚**（``cause`` 保留
                原始异常）。
        """
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MoveResult()

        src = to_uuid(src_album_uuid)
        dst = to_uuid(dst_album_uuid)

        # 守卫：往系统相册 / 回收站"移动"即使 src == dst 也是非法请求。
        self.ensure_mutable_album(src)
        self.ensure_mutable_album(dst)

        try:
            outcome = self._asset_repo.move_between_albums(src, dst, uuids)
        except Exception as exc:  # 事务已由仓储回滚，无部分生效
            raise BatchAtomicityError(
                "move_between_albums failed atomically: "
                f"src={src} dst={dst} count={len(uuids)}",
                cause=exc,
            ) from exc

        skipped = tuple(
            SkippedAsset(
                to_uuid(item.asset_uuid),
                _MOVE_SKIP_REASON_MAP.get(
                    getattr(item, "reason", None), SkipReason.UNKNOWN,
                ),
            )
            for item in (getattr(outcome, "skipped", None) or ())
        )
        return MoveResult(
            moved=_dedupe(getattr(outcome, "moved", None) or ()),
            already_in_target=_dedupe(
                getattr(outcome, "already_in_target", None) or (),
            ),
            skipped=skipped,
        )

    def copy_into_album(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Iterable[_UUIDLike],
    ) -> MembershipResult:
        """把一批资产复制到目标相册。

        与 :meth:`add_to_album` **语义等价** —— "复制"在数据层就是"给目标相册
        加成员"（一个资产可以同时在多个相册里），因此直接委托，不复制实现。
        """
        return self.add_to_album(album_uuid, asset_uuids)

    # ------------------------------------------------------------------ #
    # 回收站
    # ------------------------------------------------------------------ #
    def empty_trash(self) -> PurgeReport:
        """清空回收站（物理删除），委托 ``TrashService``。

        物理清除报告已经是**七字段 uuid 面**，本层只做透传与字段兜底
        （``getattr``：缺什么就填保守默认值，绝不把缺失当"成功"）。

        批级异常**不向上抛**（体现在 ``completed=False`` + ``error``，且
        ``purged`` 保留已完成部分）；``TrashService`` 的致命异常（连接已
        关闭等 ``sqlite3.ProgrammingError``）按设计穿透。
        """
        report = self._trash_service.empty_trash()
        return PurgeReport(
            purged=_dedupe(getattr(report, "purged", None) or ()),
            thumb_failures=tuple(
                getattr(report, "thumb_failures", None) or (),
            ),
            requested=int(getattr(report, "requested", 0) or 0),
            skipped=_dedupe(getattr(report, "skipped", None) or ()),
            completed=bool(getattr(report, "completed", True)),
            error=getattr(report, "error", None),
            count_after=int(getattr(report, "count_after", 0) or 0),
        )

    def count_deleted(self) -> int:
        """回收站当前资产数（**调用瞬间的快照**）。

        不承诺与随后 ``empty_trash`` 的实际清理数一致（并发新增 / 过期清理
        都会改变它）；上层只用于拼确认文案，不要用于对账。
        """
        return int(self._trash_service.count_deleted())

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _album_is_visible(self, album_uuid: UUID) -> bool:
        """普通相册是否可见（已软删 → False）。"""
        return bool(self._view_repo.album_is_visible(album_uuid))

    @staticmethod
    def _asset_skips(
            classified: ClassifiedAssets,
    ) -> tuple[SkippedAsset, ...]:
        """资产级 skipped：missing + 不可见。相册级原因由调用方追加。"""
        return tuple(
            SkippedAsset(u, SkipReason.ASSET_MISSING)
            for u in classified.missing
        ) + tuple(
            SkippedAsset(u, SkipReason.ASSET_NOT_VISIBLE)
            for u in classified.deleted
        )

    def _skip_all(
            self,
            classified: ClassifiedAssets,
            album_reason: SkipReason,
    ) -> tuple[SkippedAsset, ...]:
        """相册级不可用时，按"资产级原因优先 + 相册级原因"分类全部输入。"""
        return self._asset_skips(classified) + tuple(
            SkippedAsset(u, album_reason) for u in classified.visible
        )
