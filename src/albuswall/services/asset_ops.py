#
"""资产写操作业务服务（纯 Python）。

本模块是资产写操作的唯一后端入口：收藏 / 回收站 / 相册成员关系 /
清空回收站。它只做**编排**，不碰存储细节、不碰 UI。

边界（硬约束，改本文件前先读）
------------------------------
* **不 import 任何图形界面框架**（Qt 绑定 / 控件 / 对话框全不碰）：
  不发信号、不碰剪贴板 / 文件管理器 / 系统通知 / 模态弹窗；
* **不持有会话状态**：没有 undo 栈、没有剪贴板、没有"当前目标相册"、
  没有快捷槽、没有 confirm 标志——撤销、确认、文案全部由 presenter 侧
  根据返回的 DTO 决定；
* **不生成 UI 文案**：异常与 DTO 只携带 uuid 和领域枚举，人类可读文案
  归 i18n / presenter；
* **uuid 为键**：对外只接受资产 / 相册 uuid（``UUID`` 或 canonical 字符串），
  内部需要数字 id 时由本模块或依赖服务桥接，数字 id 不外泄；
* **批量优先**：除 ``set_favorite``（按语义就是单资产）外，所有写接口
  接受 uuid 集合，内部一次批量读、一次批量写；
* **结果结构化**：返回 dataclass，逐项区分
  ``changed / noop / not_found / skipped(原因)``；绝不返回 ``bool`` /
  ``int`` / 错误字符串冒充结果；
* **守卫失败抛类型化异常**（见 :mod:`albuswall.services.errors`），
  方法 docstring 逐条写明抛什么；
* **不重复造轮子**：恢复 / 清空回收站委托 ``TrashService``；标签绑定
  委托 ``AssetTagService``（本轮未暴露标签接口）；缩略图清理**不在**本
  服务内出现。

依赖缺口清单（D2）
------------------
**本轮已落地**（每条的接口名 / 语义见对应文件 docstring）：

G2  精确软删返回值 —— ``AssetRepository.soft_delete_by_uuids(uuids)``：
    单事务、只返回**实际被软删**的 uuid（``trash.changed`` 的唯一可信来源）。
G3  回收站 uuid 面恢复 —— ``TrashService.restore_by_uuid(uuid)`` /
    ``restore_by_uuids(uuids)``：uuid→id 桥接留在 ``services/trash/``，
    对外返回 ``RestoreResult``（uuid 面），冲突原因翻成枚举。
G4  清空回收站的部分结果 —— ``TrashService._purge_loop`` 捕获**批级**异常
    （致命异常仍穿透），``PurgeReport`` 七字段贯通，
    ``purged`` 与 ``skipped`` 不重叠，``count_after`` 收工重查。
G5  ~~批量成员关系查询~~ —— 由 A2 取代：
    ``AssetRepository.memberships(album_uuid, uuids) -> dict[UUID, bool]``，
    条件与 ``add_to_album`` 的 ``INSERT ... SELECT`` **同源**，键是 UUID。
G7  相册存在性 / 可见性 —— ``ViewRepository.album_exists(uuid)`` /
    ``album_is_visible(uuid)``（前者含已软删，后者等价 ``get_album`` 过滤）。
G9  真原子跨相册移动 —— ``AssetRepository.move_between_albums``：单事务、
    事务内预读、``src == dst`` 早退不写行、失败整体回滚、异常原样穿透
    （本服务翻译成 ``BatchAtomicityError``）。
G10 ~~id → uuid 桥接~~ —— **不再需要**：物理清除报告已收敛为 uuid 面
    （``PurgeCandidate.uuid``），``empty_trash`` 不再回填数字 id。
G11 资产三分类一次查询 —— ``ViewRepository.classify_by_uuids(uuids)``，
    返回 ``{canonical uuid str: "active" | "deleted"}``，一次 ``IN (...)``。
G13 系统相册 / 回收站相册判定 —— 哨兵 uuid 下沉
    ``common/constants/albums.py``（唯一真源）；``ViewRepository.is_system_album``
    / ``is_trash_album`` 与 ``ui/builtin/library.py`` 现有判定等义。

**仍缺（本轮范围外，调用时抛 ``NotImplementedError``）**：

G1  收藏读写
    ``AssetRepository.get_by_uuid(uuid) -> asset | None``（asset 至少暴露
    ``is_favorite``）。现状：``set_favorite`` 只返回 rowcount，无法区分
    ``updated`` / ``noop`` / ``not_found``；单资产读只有带可见性过滤的
    ``ViewRepository.get_asset``。

G6  批量加 / 减相册并返回实际变化
    ``AssetRepository.add_to_album(album_uuid, asset_uuids) -> set[UUID]``、
    ``remove_from_album(album_uuid, asset_uuids) -> set[UUID]``。
    现状：单资产版返回 int rowcount；``added`` / ``removed`` 必须来自实际
    变化，不能用输入冒充。**注意**：``move_between_albums`` 已经在事务内
    自带批量写入，``add_to_album`` / ``remove_from_album`` / ``copy``
    仍走单资产接口，因此这三个方法目前止步于此。

G8  资产批量存在性（含已软删）
    ``AssetRepository.existing_uuids(uuids) -> set[UUID]``。
    现状：只能逐条 ``get_asset(include_deleted=True)``；
    ``remove_from_album`` 允许清理已软删资产的成员关系，不能复用
    "仅可见"的 ``memberships`` 判定。

G12 单资产可见性
    ``ViewRepository.get_visibility(uuid) -> str | None``。
    现状：``get_asset`` 返回 DTO 或 None，"不存在"与"不可见"混为一谈，
    而 ``ensure_asset_visible`` 必须让两者抛不同异常。

G14 缩略图失败项的数字 id
    ``dto.trash.ThumbFailure.asset_id`` 是 ``assets.id``（DB 行删除后才
    生成，无法再反查 uuid），因此 ``PurgeReport.thumb_failures`` 按
    **透传**处理。根治需要 schema 侧留快照列，P3。

逆操作原语（B12）
-----------------
本服务不实现撤销栈，但要保证每一组写操作都有**语义对称的逆操作**，
presenter 才能自己记账：

===============================  ==========================================
正向                             逆操作
===============================  ==========================================
``trash(uuids)``                 ``restore(changed)``——只回灌 ``changed``
``restore(uuids)``               ``trash(restored)``
``move(src, dst, uuids)``        ``move(dst, src, moved ∪ already_in_target)``
``add_to_album(album, uuids)``   ``remove_from_album(album, added)``
``remove_from_album(album, uuids)``  ``add_to_album(album, removed)``
``set_favorite(uuid, v)``        ``set_favorite(uuid, previous_value)``
``empty_trash()``                **不可逆**（物理删除，无逆操作）
===============================  ==========================================

注意逆操作只对**返回值里那一列**回灌：``trash`` 用 ``changed``、
``add`` 用 ``added``、``remove`` 用 ``removed``——用输入集合回灌会把
本来没变的项也翻一遍。

原子性 / 事务边界总表
---------------------
===============================  ===================================
方法                              原子性
===============================  ===================================
``set_favorite``                  单行写，原子
``trash``                         批量 UPDATE（**批内原子**），但
                                  "先分类后写"的两步之间非原子
``restore``                       逐条，**不整批回滚**（一条冲突不影响其他条）
``add_to_album``                  逐条/批量混合，**不保证整批原子**
``remove_from_album``             同上
``copy_into_album``               = ``add_to_album``
``move_between_albums``           **真原子**：失败抛 ``BatchAtomicityError``，全回滚
``empty_trash``                   分批短事务，**部分完成可观测**（``completed``）
``classify_assets``               只读快照，不承诺与后续写之间无并发变化
===============================  ===================================

实现顺序建议（D3）
------------------
1. DTO + errors 先落（本文件依赖的返回结构 / 异常类型锁定）。
2. G11 + G12：``classify_by_uuids`` / ``get_visibility``——所有写接口的前置原语。
3. G1：``set_favorite``（最简单，可端到端验证 DTO 语义）。
4. G2 → ``trash``；G3 → ``restore``（回收站闭环）。
5. G5/G6/G7/G8 → ``add`` / ``remove`` / ``copy``（非原子，先跑通）。
6. G9 → ``move_between_albums``（真原子，最后做）。
7. G4/G10 → ``empty_trash`` / ``count_deleted``。
8. G13 与标签接口（``AssetTagService`` 组合）按 UI 需求后补。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol
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
    SkippedAsset,
    SkipReason,
    TrashResult,
)
from albuswall.dto.utils import to_uuid
from albuswall.services.errors import (
    AlbumNotFound,
    AssetNotFound,
    BatchAtomicityError,
    NotAMutableAlbum,
    NotAVisibleAsset,
)

__all__ = [
    "AssetOpsService",
    "AssetViewRepository",
    "AssetWriteRepository",
    "TrashServicePort",
]

# 入参统一接受 UUID 或 canonical uuid 字符串（与 DTO 层 to_uuid 同口径）；
# 返回值 / DTO 字段一律是 UUID。
_UUIDLike = UUID | str

# 可见性状态词表。项目既有 scope 用语是 "active"/"deleted"（见
# services/view.py 的 SCOPE_ACTIVE / SCOPE_DELETED），个别仓储写
# "visible"，两者都认，避免为一个词改动底层。
_VISIBLE_STATES = frozenset({"visible", "active"})
_DELETED_STATES = frozenset({"deleted"})


# ======================================================================
# 依赖契约（Protocol）
#
# 描述"AssetOpsService 期望底层提供什么"，不是当前实现的拷贝。方法缺失
# 时运行期由 ``_require`` 抛 NotImplementedError（见模块 docstring 的缺口
# 清单），Protocol 只负责让类型检查器 / 读者看清契约。
# ======================================================================
class AssetWriteRepository(Protocol):
    """AssetWriteRepository"""
    def get_by_uuid(self, asset_uuid: _UUIDLike) -> Any:
        """        返回资产实体（至少暴露 ``is_favorite``），不存在返回 None。"""
        ...
    def set_favorite(self, asset_uuid: _UUIDLike, value: bool) -> None:
        """        把 ``is_favorite`` 写成 ``value``。"""
        ...
    def soft_delete_by_uuids(self, asset_uuids: Sequence[_UUIDLike]) -> set[UUID]:
        """        批量软删，返回**实际被软删**的 uuid 集合。"""
        ...
    def existing_uuids(self, asset_uuids: Sequence[_UUIDLike]) -> set[UUID]:
        """        返回实际存在的 uuid（**含已软删**）。"""
        ...
    def memberships(
        self, album_uuid: _UUIDLike, asset_uuids: Sequence[_UUIDLike]
    ) -> dict[UUID, bool]:
        """        批量判定成员关系；键为归一化 UUID，条件与 ``add_to_album`` 同源。"""
        ...
    def add_to_album(
        self, album_uuid: _UUIDLike, asset_uuids: Sequence[_UUIDLike]
    ) -> set[UUID]:
        """        批量加成员，返回实际新增的 uuid。"""
        ...
    def remove_from_album(
        self, album_uuid: _UUIDLike, asset_uuids: Sequence[_UUIDLike]
    ) -> set[UUID]:
        """        批量删成员，返回实际移除的 uuid。"""
        ...
    def move_between_albums(
        self,
        src_album_uuid: _UUIDLike,
        dst_album_uuid: _UUIDLike,
        asset_uuids: Sequence[_UUIDLike],
    ) -> Any:
        """        单事务移动；返回 repo 层 ``MoveOutcome``（moved/already/skipped）。

        失败整体回滚并抛异常（不吞、不翻译），由服务层转
        ``BatchAtomicityError``。"""
        ...

class AssetViewRepository(Protocol):
    """AssetViewRepository"""
    def classify_by_uuids(self, asset_uuids: Sequence[_UUIDLike]) -> Any:
        """        返回 ``{canonical uuid str: "active"|"deleted"}``；缺失即不存在。"""
        ...
    def get_visibility(self, asset_uuid: _UUIDLike) -> str | None:
        """        返回可见性状态；uuid 不存在返回 None。"""
        ...
    def album_exists(self, album_uuid: _UUIDLike) -> bool:
        """        相册行是否存在（含已软删）。"""
        ...
    def album_is_visible(self, album_uuid: _UUIDLike) -> bool:
        """        相册是否存在且未软删。"""
        ...
    def is_system_album(self, album_uuid: _UUIDLike) -> bool:
        """        是否系统相册（All / Trash 等虚拟相册）。"""
        ...
    def is_trash_album(self, album_uuid: _UUIDLike) -> bool:
        """        是否回收站相册。"""
        ...

class TrashServicePort(Protocol):
    """TrashServicePort"""
    def count_deleted(self) -> int:
        """        回收站当前资产数（快照）。"""
        ...
    def restore_by_uuids(self, asset_uuids: Sequence[_UUIDLike]) -> Any:
        """        uuid 版逐条恢复，返回 ``RestoreResult``（restored / not_found /
        conflicts，uuid 面）。"""
        ...
    def empty_trash(self) -> Any:
        """        清空回收站，返回七字段 ``PurgeReport``（uuid 面，含
        ``completed`` / ``error`` / ``skipped``）。"""
        ...


# ======================================================================
# 内部工具
# ======================================================================
def _dedupe(uuids: Iterable[_UUIDLike]) -> tuple[UUID, ...]:
    """        归一化为 UUID 并去重、保持首次出现顺序。

        去重放在**归一化之后**：``UUID`` 与同一个 uuid 的字符串输入会合并，
        否则同一资产会被写两次、在结果里出现两项。"""
    return tuple(dict.fromkeys(to_uuid(u) for u in uuids))


def _as_uuid_set(values: Iterable[Any] | None) -> set[UUID]:
    """        把仓储返回的 uuid 容器（可能是 UUID 也可能是 str）归一化为 set。"""
    if not values:
        return set()
    return {to_uuid(v) for v in values}


def _as_uuid_list(values: Iterable[Any] | None) -> list[UUID]:
    """        同 :func:`_as_uuid_set`，保序返回 list。"""
    if not values:
        return []
    return [to_uuid(v) for v in values]


def _require(owner: Any, name: str, gap: str) -> Any:
    """        取依赖方法；缺失说明缺口未落地，抛 ``NotImplementedError``。

        刻意不 ``return None`` 也不静默降级：写操作"假成功"比抛错危险得多
        （撤销栈会记下没发生的变更）。"""
    method = getattr(owner, name, None)
    if not callable(method):
        raise NotImplementedError(
            f"{type(owner).__name__}.{name}() 不存在：对应依赖缺口 {gap} "
            f"未落地，详见 albuswall.services.asset_ops 模块 docstring 的缺口清单。"
        )
    return method


_COLD_STATE_TO_VISIBLE = {
    **{s: "visible" for s in _VISIBLE_STATES},
    **{s: "deleted" for s in _DELETED_STATES},
}

# repo 层 ``MoveSkipReason`` 字符串 → 领域 ``SkipReason``。
# 两套词表是刻意的（D4：repo 不 import dto.asset）；这张表是唯一翻译点。
# 未知取值 → ``SkipReason.UNKNOWN``（不静默丢条）。
_MOVE_SKIP_REASON_MAP = {
    "asset_missing": SkipReason.ASSET_MISSING,
    "asset_invisible": SkipReason.ASSET_NOT_VISIBLE,
    "source_invisible": SkipReason.SOURCE_ALBUM_NOT_VISIBLE,
    "dst_album_missing": SkipReason.TARGET_ALBUM_NOT_VISIBLE,
    "dst_album_deleted": SkipReason.TARGET_ALBUM_NOT_VISIBLE,
    "not_in_source": SkipReason.ASSET_NOT_IN_SOURCE,
}


# ======================================================================
# Service
# ======================================================================
class AssetOpsService:
    """        AssetOpsService"""

    def __init__(
            self,
            asset_repo: AssetWriteRepository,
            view_repo: AssetViewRepository,
            trash_service: TrashServicePort,
            tag_service: Any = None,
    ) -> None:
        """只存依赖引用：不建连接、不读配置、不做 IO、不启动后台线程。"""
        self._asset_repo = asset_repo
        self._view_repo = view_repo
        self._trash_service = trash_service
        # 预留：将来批量打标签时组合 AssetTagService，而不是抄它的 repo 调用。
        # 当前所有公开接口都不使用它。
        self._tag_service = tag_service

    # ------------------------------------------------------------------ #
    # 守卫与分类
    # ------------------------------------------------------------------ #
    def classify_assets(self, asset_uuids: Iterable[_UUIDLike]) -> ClassifiedAssets:
        """        一次批量查出 visible / deleted / missing 三类资产。

        做什么：先去重保序，再调 ``view_repo.classify_by_uuids`` 一次查出
        每条的可见性状态，按三类互斥分桶。
        返回：:class:`ClassifiedAssets`，三类互斥，并集 = 去重后的输入
        （不额外扩展）。
        抛：底层仓储异常；缺口 G11 未落地时抛 ``NotImplementedError``。
        前置条件：无（纯读）。
        原子性：**非原子**——只是只读快照，不保证与后续写操作之间没有
        并发变化；写接口因此始终以仓储的"实际变化返回值"记账。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return ClassifiedAssets()

        classify = _require(self._view_repo, "classify_by_uuids", "G11")
        rows = classify(uuids) or {}

        # 契约键是 canonical str；这里再 to_uuid 归一化，容忍 UUID 键的
        # 实现，避免 dict.get(UUID) 静默 miss 把一切判成 missing。
        state_by_uuid = {to_uuid(k): v for k, v in dict(rows).items()}

        visible: list[UUID] = []
        deleted: list[UUID] = []
        missing: list[UUID] = []
        for u in uuids:
            state = state_by_uuid.get(u)
            if state is None:
                missing.append(u)
                continue
            normalized = _COLD_STATE_TO_VISIBLE.get(str(state))
            if normalized == "visible":
                visible.append(u)
            elif normalized == "deleted":
                deleted.append(u)
            else:
                # 未定义状态保守归为 missing：宁可跳过，不可误写。
                missing.append(u)

        return ClassifiedAssets(
            visible=tuple(visible),
            deleted=tuple(deleted),
            missing=tuple(missing),
        )

    def ensure_asset_visible(self, asset_uuid: _UUIDLike) -> None:
        """        确保资产存在且可见。

        做什么：读一次可见性状态并判定。
        返回：None（成功即通过）。
        抛：
          * :class:`AssetNotFound`——uuid 不存在；
          * :class:`NotAVisibleAsset`——存在但不可见（已软删 / 源已下线），
            ``reason`` 为底层状态字符串。
        前置条件：无。
        原子性：非原子只读判定（"检查后使用"期间状态可变；真正的写入
        仍由各写接口按实际返回值记账）。"""
        uuid = to_uuid(asset_uuid)
        get_visibility = _require(self._view_repo, "get_visibility", "G12")
        state = get_visibility(uuid)
        if state is None:
            raise AssetNotFound(uuid)
        if str(state) not in _VISIBLE_STATES:
            raise NotAVisibleAsset(uuid, reason=str(state))

    def ensure_mutable_album(self, album_uuid: _UUIDLike) -> None:
        """        确保相册存在且不是系统相册 / 回收站相册。

                做什么：先判系统/回收站（虚拟相册），再判存在性。
                返回：None。
                抛：
                  * :class:`AlbumNotFound`——uuid 不对应任何相册行（含非法 uuid）；
                  * :class:`NotAMutableAlbum`——回收站相册（``reason="trash"``）
                    或其它系统相册（``reason="system"``）。

                前置条件：无。
                原子性：非原子只读判定。
                说明：本方法**不**检查普通相册是否可见；不可见相册由各写接口
                转成逐项 ``skipped(TARGET_ALBUM_NOT_VISIBLE)``。
                顺序：系统/回收站判定**先于**存在性判定——虚拟相册（All / Trash）
                不落 ``albums`` 表，若先查存在性会把 Trash 误判成
                ``AlbumNotFound``，丢掉更具体的 ``reason="trash"``。
"""
        uuid = to_uuid(album_uuid)
        # 先判回收站再判系统相册：回收站相册同时也是虚拟/系统相册，
        # 先判更具体的那个，reason 才有区分度（"trash" 比 "system" 有用）。
        is_trash = _require(self._view_repo, "is_trash_album", "G13")
        if is_trash(uuid):
            raise NotAMutableAlbum(uuid, reason="trash")
        is_system = _require(self._view_repo, "is_system_album", "G13")
        if is_system(uuid):
            raise NotAMutableAlbum(uuid, reason="system")
        # 系统相册是**虚拟**的（不落 albums 表），因此存在性判定必须放在
        # 系统/回收站判定之后——否则 Trash 会先撞 AlbumNotFound，reason 丢失。
        album_exists = _require(self._view_repo, "album_exists", "G7")
        if not album_exists(uuid):
            raise AlbumNotFound(uuid)

    def is_system_album(self, album_uuid: _UUIDLike) -> bool:
        """        相册是否为系统相册（All / Trash 等虚拟相册）。

        做什么：领域布尔判定，供 presenter 决定菜单可用性。
        返回：bool。
        抛：缺口 G13 未落地时抛 ``NotImplementedError``。
        前置条件：无。
        原子性：非原子只读。"""
        is_system = _require(self._view_repo, "is_system_album", "G13")
        return bool(is_system(to_uuid(album_uuid)))

    def is_trash_album(self, album_uuid: _UUIDLike) -> bool:
        """        相册是否为回收站相册。

        做什么 / 返回 / 抛 / 前置 / 原子性：同 :meth:`is_system_album`。"""
        is_trash = _require(self._view_repo, "is_trash_album", "G13")
        return bool(is_trash(to_uuid(album_uuid)))

    # ------------------------------------------------------------------ #
    # 核心写操作
    # ------------------------------------------------------------------ #
    def set_favorite(self, asset_uuid: _UUIDLike, favorite: bool) -> FavoriteResult:
        """        设置 / 取消资产收藏。

        做什么：读旧值 → 相同则不动 → 不同则写。
        返回：:class:`FavoriteResult`，区分 ``updated``（``previous_value``
        供撤销）/ ``noop``（未写库）/ ``not_found``。
        抛：**无守卫异常**（不存在走 ``not_found``）；仓储异常向上抛；
        缺口 G1 未落地时抛 ``NotImplementedError``。
        前置条件：无——收藏对已软删资产同样合法（回收站里也能取消收藏）。
        原子性：单行写，原子。读旧值与写入之间是两次操作，但收藏是幂等
        开关，并发下的最终值以最后一次写为准，不产生中间态。"""
        uuid = to_uuid(asset_uuid)
        get_by_uuid = _require(self._asset_repo, "get_by_uuid", "G1")
        asset = get_by_uuid(uuid)
        if asset is None:
            return FavoriteResult(asset_uuid=uuid, outcome="not_found")

        current = bool(asset.is_favorite)
        if current == bool(favorite):
            return FavoriteResult(
                asset_uuid=uuid,
                outcome="noop",
                previous_value=current,
            )

        set_favorite = _require(self._asset_repo, "set_favorite", "G1")
        set_favorite(uuid, bool(favorite))
        return FavoriteResult(
            asset_uuid=uuid,
            outcome="updated",
            previous_value=current,
        )

    def trash(self, asset_uuids: Iterable[_UUIDLike]) -> TrashResult:
        """        批量软删资产到回收站。

        做什么：先批量分类，再对 visible 子集批量软删。
        返回：:class:`TrashResult`，三列**互斥**：

        * ``changed``——真正被软删的 uuid。**撤销记录只能取这一列**；
        * ``already_deleted``——此前已在回收站的 uuid；
        * ``missing``——不存在的 uuid。

        抛：底层仓储异常向上抛（非守卫异常：trash 不因个别资产不可见而
        整批失败）；缺口 G2/G11 未落地时抛 ``NotImplementedError``。
        前置条件：无。
        原子性：软删本身是单条批量 UPDATE（批内原子）；但"先分类后写"
        两步之间非原子——并发下某资产可能在分类后被删除，此时仓储返回的
        ``changed`` 不会包含它，结果仍自洽（我们只信返回值，不信输入）。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return TrashResult()

        classified = self.classify_assets(uuids)
        changed: set[UUID] = set()
        if classified.visible:
            soft_delete = _require(
                self._asset_repo, "soft_delete_by_uuids", "G2",
            )
            changed = _as_uuid_set(soft_delete(classified.visible))

        return TrashResult(
            changed=tuple(u for u in classified.visible if u in changed),
            already_deleted=classified.deleted,
            missing=classified.missing,
        )

    def restore(self, asset_uuids: Iterable[_UUIDLike]) -> RestoreResult:
        """        从回收站恢复一批资产。

        做什么：委托 ``TrashService`` 的 uuid 版逐条恢复，把底层结果翻译成
        uuid 面 DTO。
        返回：:class:`RestoreResult`（``restored`` / ``not_found`` /
        ``conflicts``）。
        抛：**无守卫异常**，冲突走结果；底层逐条恢复的其它异常向上抛；
        缺口 G3 未落地时抛 ``NotImplementedError``。
        前置条件：资产当前在回收站（未在回收站的会在 ``not_found`` 里，
        不抛异常）。
        原子性：**逐条处理，不整批回滚**——一条唯一约束冲突不影响其他条
        恢复成功。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return RestoreResult()

        # TODO(G3)：TrashService 已提供 uuid 面 restore_by_uuids（本层只做
        # 结果规整）；若底层退回 id 面，这里会因为 _conflict_uuid 拿不到
        # uuid 而响亮失败，而不是静默错值。
        restore_by_uuids = _require(
            self._trash_service, "restore_by_uuids", "G3",
        )
        outcome = restore_by_uuids(uuids)

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
        """        把一批资产加入目标相册（只加不删）。

        做什么：守卫相册 → 分类资产 → 预读"本已在相册"的成员 → 批量加。
        返回：:class:`MembershipResult`：

        * ``added``——实际新增的 uuid（来自仓储返回值，不是输入）；
        * ``existing``——本来就在目标相册的 uuid；
        * ``skipped``——未处理的 uuid + 原因，覆盖
          ``ASSET_MISSING`` / ``ASSET_NOT_VISIBLE`` /
          ``TARGET_ALBUM_NOT_VISIBLE``。

        抛：
          * :class:`AlbumNotFound`——目标相册不存在；
          * :class:`NotAMutableAlbum`——目标相册是系统 / 回收站相册；
          * 缺口 G5/G6/G7/G11/G13 未落地时抛 ``NotImplementedError``。

        前置条件：无（不可见资产被跳过，不抛）。目标相册不可见（已软删）
        时**不抛**，全部 ``skipped(TARGET_ALBUM_NOT_VISIBLE)``。

        原子性：批量 INSERT OR IGNORE（批内原子），但"预读 existing → 写入"
        非原子，故**不保证整批原子**；``added`` 以仓储实际返回为准。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MembershipResult()

        album = to_uuid(album_uuid)
        self.ensure_mutable_album(album)
        classified = self.classify_assets(uuids)

        if not self._album_is_visible(album):
            return MembershipResult(
                skipped=self._skip_all(classified, SkipReason.TARGET_ALBUM_NOT_VISIBLE),
            )

        existing: set[UUID] = set()
        if classified.visible:
            memberships = _require(self._asset_repo, "memberships", "A2")
            existing = {
                uuid for uuid, is_member in memberships(
                    album, classified.visible,
                ).items() if is_member
            }

        to_add = [u for u in classified.visible if u not in existing]
        added: set[UUID] = set()
        if to_add:
            add = _require(self._asset_repo, "add_to_album", "G6")
            added = _as_uuid_set(add(album, to_add))

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
        """        把一批资产从目标相册移除（只删不加）。

        做什么：守卫相册 → 分类（仅为区分 missing）→ 预读是否在相册 → 批量删。
        返回：:class:`MembershipResult`：

        * ``removed``——实际移除的 uuid（来自仓储返回值）；
        * ``not_present``——本来就不在该相册的 uuid；
        * ``skipped``——不存在的 uuid（``ASSET_MISSING``）。

        抛：同 :meth:`add_to_album`（存在性 / 可写性守卫 + 缺口未落地）。
        前置条件：无。
        语义说明：**不要求资产当前可见**——允许从相册清掉已软删资产的
        成员关系（回收站里的资产不该继续占着相册），因此这里用
        ``existing_uuids``（含已软删）判存在性，而不是 visible 分类；
        只有"根本不存在"才 skipped。

        原子性：批量 DELETE（批内原子），"预读 → 删除"非原子，
        **不保证整批原子**。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MembershipResult()

        album = to_uuid(album_uuid)
        self.ensure_mutable_album(album)

        if not self._album_is_visible(album):
            # 相册不可见时也无法得知成员关系，统一按资产存在性分类后 skip。
            classified = self.classify_assets(uuids)
            return MembershipResult(
                skipped=self._skip_all(classified, SkipReason.TARGET_ALBUM_NOT_VISIBLE),
            )

        existing_uuids = _require(self._asset_repo, "existing_uuids", "G8")
        present = _as_uuid_set(existing_uuids(uuids))
        candidates = [u for u in uuids if u in present]
        missing = [u for u in uuids if u not in present]

        in_album: set[UUID] = set()
        if candidates:
            memberships = _require(self._asset_repo, "memberships", "A2")
            in_album = {
                uuid for uuid, is_member in memberships(
                    album, candidates,
                ).items() if is_member
            }

        to_remove = [u for u in candidates if u in in_album]
        not_present = [u for u in candidates if u not in in_album]

        removed: set[UUID] = set()
        if to_remove:
            remove = _require(self._asset_repo, "remove_from_album", "G6")
            removed = _as_uuid_set(remove(album, to_remove))

        return MembershipResult(
            removed=tuple(u for u in to_remove if u in removed),
            not_present=tuple(not_present),
            skipped=tuple(SkippedAsset(u, SkipReason.ASSET_MISSING) for u in missing),
        )

    def move_between_albums(
            self,
            src_album_uuid: _UUIDLike,
            dst_album_uuid: _UUIDLike,
            asset_uuids: Iterable[_UUIDLike],
    ) -> MoveResult:
        """        把一批资产从源相册移动到目标相册。

        做什么：守卫两个相册 → 把整批交给 ``AssetRepository.move_between_albums``
        做**单事务**移动（事务内预读、源删 + 目标补）→ 把 repo 层结果翻译成
        对外 DTO。
        返回：:class:`MoveResult`（``moved`` / ``already_in_target`` /
        ``skipped``）。
        抛：
          * :class:`AlbumNotFound`——源或目标相册不存在（本层守卫）；
          * :class:`NotAMutableAlbum`——源或目标是系统 / 回收站相册；
          * :class:`BatchAtomicityError`——仓储事务失败，**整批已回滚**；
          * 缺口 G9 未落地（repo 无 ``move_between_albums``）时抛
            ``NotImplementedError``。

        前置条件：无。源/目标相册**在守卫通过后**并发被软删时**不抛**，
        由仓储把整批标成 skipped（``TARGET_ALBUM_NOT_VISIBLE`` 优先于
        ``SOURCE_ALBUM_NOT_VISIBLE``）。

        原子性：**真原子**——要么全部生效，要么全部不生效。服务层**不**
        循环调用 add/remove，只调一次 ``repo.move_between_albums``
        （单事务，失败由仓储整体回滚后重抛）；本层只负责把异常翻译成
        ``BatchAtomicityError``（``cause`` 保留原始异常）。

        语义细节（与 repo 的 ``MoveOutcome`` 对齐）：

        * ``src == dst`` 的早退在**仓储内部**（显式分支、不写任何行；
          ``noop_reason = src_equals_dst``）。本层不重复判定，避免两处
          语义漂移；可见资产若非该相册成员，归
          ``skipped(ASSET_NOT_IN_SOURCE)``；
        * ``already_in_target`` 是 ``moved`` 的**子集**（既在源又在目标的
          资产只从源侧摘掉，不重复插入）；
        * 资产级 skipped 来自仓储的逐条预读结果，本层只做原因词表翻译
          （repo 词汇见 ``AssetRepository.MoveSkipReason``）。"""
        uuids = _dedupe(asset_uuids)
        if not uuids:
            return MoveResult()

        src = to_uuid(src_album_uuid)
        dst = to_uuid(dst_album_uuid)

        # 守卫：往系统相册 / 回收站"移动"即使 src == dst 也是非法请求。
        self.ensure_mutable_album(src)
        self.ensure_mutable_album(dst)

        move = _require(self._asset_repo, "move_between_albums", "A3/G9")
        try:
            outcome = move(src, dst, uuids)
        except NotImplementedError:
            raise
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
                    item.reason, SkipReason.UNKNOWN,
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
        """        把一批资产复制到目标相册。

        做什么：与 :meth:`add_to_album` **语义等价**——"复制"在数据层就是
        "给目标相册加成员"，源相册的成员关系与新加的成员关系互不影响
        （一个资产可以同时在多个相册里），因此直接委托，不复制实现。
        返回 / 抛 / 前置 / 原子性：同 :meth:`add_to_album`。"""
        return self.add_to_album(album_uuid, asset_uuids)

    # ------------------------------------------------------------------ #
    # 回收站
    # ------------------------------------------------------------------ #
    def empty_trash(self) -> PurgeReport:
        """        清空回收站（物理删除），委托 ``TrashService``。

                做什么：直接委托 ``TrashService.empty_trash()``。物理清除报告
                （``PurgeReport``）在 D3 后已经是**七字段 uuid 面**，本层不再做
                id→uuid 回填，只做透传与字段兜底。
                返回：:class:`PurgeReport`（``purged`` / ``thumb_failures`` /
                ``requested`` / ``skipped`` / ``completed`` / ``error`` /
                ``count_after``）。``purged`` 与 ``skipped`` **不重叠**。
                抛：**批级异常不向上抛**（体现在 ``completed=False`` + ``error``，
                且 ``purged`` 保留已完成部分）；``TrashService`` 的致命异常
                （连接已关闭等 ``sqlite3.ProgrammingError``）按设计穿透。
                前置条件：无（不接收 ``confirm`` 参数：二次确认是 UI 的事；
                计数快照由底层自己取，本层不重复查询）。
                原子性：分批短事务，**部分完成可观测**——见
                ``TrashService._purge_loop`` 的异常语义。
"""
        empty = _require(self._trash_service, "empty_trash", "G4")
        report = empty()

        # 字段兜底用 getattr：底层契约已对齐，但旧实现可能只给两个字段；
        # 缺什么就填保守默认值，绝不把缺失当"成功"。
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
        """        回收站当前资产数（快照值）。

        做什么：透传 ``TrashService.count_deleted``。
        返回：int。
        抛：底层异常向上抛。
        前置条件：无。
        语义：这是**调用瞬间的快照**，不承诺与随后 ``empty_trash`` 的实际
        清理数一致（并发新增 / 过期清理都会改变它）；上层只用于拼确认
        文案，**不要**用于对账。
        原子性：单次查询，非事务。"""
        count_deleted = _require(self._trash_service, "count_deleted", "G4/注入")
        return int(count_deleted())

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _album_is_visible(self, album_uuid: UUID) -> bool:
        """        内部：普通相册是否可见（已软删 → False）。缺口 G7。"""
        album_is_visible = _require(self._view_repo, "album_is_visible", "G7")
        return bool(album_is_visible(album_uuid))

    @staticmethod
    def _asset_skips(classified: ClassifiedAssets) -> tuple[SkippedAsset, ...]:
        """        资产级 skipped：missing + 不可见。相册级原因由调用方追加。"""
        return tuple(
            SkippedAsset(u, SkipReason.ASSET_MISSING) for u in classified.missing
        ) + tuple(
            SkippedAsset(u, SkipReason.ASSET_NOT_VISIBLE) for u in classified.deleted
        )

    def _skip_all(
            self,
            classified: ClassifiedAssets,
            album_reason: SkipReason,
    ) -> tuple[SkippedAsset, ...]:
        """        相册级不可用时，把全部输入按"资产级原因优先 + 相册级原因"分类。"""
        return self._asset_skips(classified) + tuple(
            SkippedAsset(u, album_reason) for u in classified.visible
        )


# ======================================================================
# 模块内小工具（restore 的冲突原因翻译放在类外，保持方法体短）
# ======================================================================
_CONFLICT_REASON_MAP = {
    "path_conflict": RestoreConflictReason.PATH_CONFLICT,
    "unique_constraint": RestoreConflictReason.UNIQUE_CONSTRAINT,
    "invalid_state": RestoreConflictReason.INVALID_STATE,
}


def _to_conflict_reason(raw: Any) -> RestoreConflictReason:
    """        把底层冲突原因（可能是枚举 / 字符串 / None）翻译成领域枚举。

        现状底层只有 ``"path_conflict"``；初稿直接写
        ``RestoreConflictReason(c.reason)``，遇到该字符串会抛 ``ValueError``
        ——这里降级到 ``UNKNOWN`` 而不是让恢复整体失败。"""
    if isinstance(raw, RestoreConflictReason):
        return raw
    if raw is None:
        return RestoreConflictReason.UNKNOWN
    return _CONFLICT_REASON_MAP.get(str(raw), RestoreConflictReason.UNKNOWN)


def _conflict_uuid(conflict: Any) -> Any:
    """        从底层 conflict 对象取 uuid 字段（兼容 asset_uuid / uuid / asset_id）。

        ``asset_id`` 是数字 id：``TrashService`` 的 id 面结果就是这种形状，
        缺口 G3 要求 uuid 化后这里应当只出现 uuid；在 G3 落地前，返回
        ``asset_id`` 会让 ``to_uuid`` 抛错——这是**预期内的响亮失败**
        （提示缺口未落地），不是静默错值。"""
    for attr in ("asset_uuid", "uuid", "asset_id"):
        value = getattr(conflict, attr, None)
        if value is not None:
            return value
    raise NotImplementedError(
        "依赖缺口 G3 未落地：restore 冲突项既无 asset_uuid 也无 uuid，"
        "底层只给了数字 asset_id，无法在 uuid 面表达。"
    )
