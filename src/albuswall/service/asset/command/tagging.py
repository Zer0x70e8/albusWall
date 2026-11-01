#
"""资产 × 标签绑定（``asset_tags`` 的唯一写入口）。

与 :mod:`albuswall.service.tag` 的分工
--------------------------------------
* ``TagService``      管标签自身的生命周期（create / rename / move / delete / 树）；
* ``AssetTagService`` 管「哪个资产挂了哪些标签」（增删 / 全量设置 / 查询）。

消歧义（为什么要这一层）
------------------------
``AssetRepository.add_tags_to_asset`` 返回的 0 行有两种可能：**已经绑过了**
还是**资产/标签本不可见**？仓储不区分（它只报"写了几行"）。本服务先查资产、
过滤标签，把 0 行翻译成结构化结果（``added`` / ``existing`` / ``skipped_uuids``），
让撤销栈与 UI 都能如实记账。

资产存在性走 ``ViewRepository.get_asset(include_deleted=False)``（仓储层没有
``exists`` 方法，而"可见性"本来就是 view 侧的口径）；标签存在性走
``TagRepository.exists``（默认过滤软删）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

from albuswall.dto.album import Asset
from albuswall.dto.tag.query import Tag
from albuswall.log import Logger, getLogger
from albuswall.repositories.utils.types import UuidKeyed
from albuswall.service.errors import AssetNotFound, AssetOpsError

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.repositories.asset import AssetRepository
    from albuswall.repositories.tag import TagRepository
    from albuswall.repositories.view import ViewRepository

__all__ = [
    "AddTagsResult",
    "AssetTagError",
    "AssetTagService",
    "SetTagsResult",
]

_UUIDLike = UUID | str

_logger: Logger = getLogger(__name__)

#: 服务层异常基类的别名。``AssetNotFound`` 是 ``AssetOpsError`` 的子类，
#: 因此 ``except AssetTagError`` 能同时捕获两者（与 AssetOpsService 共用一份
#: 定义，见 ``service/errors.py``）。
AssetTagError = AssetOpsError


@dataclass(frozen=True, slots=True)
class AddTagsResult:
    """``add`` 的结果：写进去了几条、本来就有几条、被跳过哪些标签。"""

    added: int
    existing: int  # 已存在绑定（幂等跳过）
    skipped_uuids: tuple[_UUIDLike, ...]  # 不存在或软删的标签


@dataclass(frozen=True, slots=True)
class SetTagsResult:
    """``set`` 的结果：新增 / 移除 / 本来就有 / 被跳过的标签。"""

    added: int
    removed: int
    existing: int
    skipped_uuids: tuple[_UUIDLike, ...]


class AssetTagService:
    """资产 × 标签的绑定编排（被动服务：只写 ``asset_tags``，不启动任何后台行为）。

    线程语义：本服务不做线程切换 —— 它只写 ``asset_tags``，而资产/标签的
    可见性判定是纯读；没有"属主线程状态"需要保护（对比 ``TrackingState``）。
    需要跨线程安全的调用方直接调用即可。
    """

    def __init__(
            self,
            asset_repo: "AssetRepository",
            tag_repo: "TagRepository",
            view_repo: "ViewRepository",
    ) -> None:
        self._asset = asset_repo
        self._tag = tag_repo
        self._view = view_repo

    def describe(self, *, verbose: bool = False) -> str:
        base = "AssetTagService()"
        if verbose:
            base = base[:-1] + (
                f"asset={type(self._asset).__name__}, "
                f"tag={type(self._tag).__name__}, "
                f"view={type(self._view).__name__})"
            )
        return base

    # ---------------------------------------------------------------- #
    # 写
    # ---------------------------------------------------------------- #
    def add(
            self,
            asset_uuid: _UUIDLike,
            tag_uuids: Sequence[_UUIDLike],
    ) -> AddTagsResult:
        """给资产打标签。

        前置：资产必须可见；不存在的标签被**静默跳过**并在结果里列出
        （一次请求里混进几个坏标签不该让整批失败）。

        返回：实际新增 / 已存在 / 被跳过的数量与 uuid。
        """
        if self._view.get_asset(asset_uuid, include_deleted=False) is None:
            raise AssetNotFound(str(asset_uuid))

        unique = list(dict.fromkeys(tag_uuids))
        valid: list[_UUIDLike] = []
        skipped: list[_UUIDLike] = []
        for u in unique:
            if self._tag.exists(u):
                valid.append(u)
            else:
                skipped.append(u)

        if not valid:
            return AddTagsResult(
                added=0, existing=0, skipped_uuids=tuple(skipped),
            )

        added = self._asset.add_tags_to_asset(asset_uuid, valid)
        return AddTagsResult(
            added=added,
            existing=len(valid) - added,
            skipped_uuids=tuple(skipped),
        )

    def remove(
            self,
            asset_uuid: _UUIDLike,
            tag_uuids: Sequence[_UUIDLike],
    ) -> int:
        """移除绑定；软删标签的绑定保留（与 repo 语义一致）。"""
        return self._asset.remove_tags_from_asset(asset_uuid, tag_uuids)

    def set(
            self,
            asset_uuid: _UUIDLike,
            tag_uuids: Sequence[_UUIDLike],
    ) -> SetTagsResult:
        """全量设置：按当前绑定 diff 出新增和移除。

        非原子（add / remove 各自带事务），但整体**幂等**：中途失败后重试会
        收敛到目标状态。
        """
        if self._view.get_asset(asset_uuid, include_deleted=False) is None:
            raise AssetNotFound(str(asset_uuid))

        # repo 返回 UUID；UI 传来的可能是 str。统一成 canonical str 再 diff。
        current = {str(u) for u in self._tag.get_tag_uuids_of_asset(asset_uuid)}
        target = {str(u) for u in tag_uuids}
        to_add = [
            u for u in dict.fromkeys(tag_uuids) if str(u) not in current
        ]
        to_remove = [u for u in current if u not in target]

        add_result = (
            self.add(asset_uuid, to_add) if to_add
            else AddTagsResult(0, 0, ())
        )
        removed = self.remove(asset_uuid, to_remove) if to_remove else 0

        return SetTagsResult(
            added=add_result.added,
            removed=removed,
            existing=add_result.existing,
            skipped_uuids=add_result.skipped_uuids,
        )

    # ---------------------------------------------------------------- #
    # 读（转发，方便 presenter 只注一个 service）
    # ---------------------------------------------------------------- #
    def list_of_asset(self, asset_uuid: _UUIDLike) -> list[Tag]:
        """资产挂了哪些标签（不含软删标签）。"""
        return self._tag.get_tags_of_asset(asset_uuid)

    def count_for_tag(self, tag_uuid: _UUIDLike) -> int:
        """某个标签下有多少资产。"""
        return self._tag.count_assets(tag_uuid)

    def count_for_tags(
            self, tag_uuids: Sequence[_UUIDLike],
    ) -> UuidKeyed[int]:
        """批量版（一次查询，避免 N+1）。"""
        return self._tag.count_assets_bulk(tag_uuids)

    def list_assets_of_tag(
            self,
            tag_uuid: _UUIDLike,
            offset: int = 0,
            limit: int = 100,
    ) -> list[Asset]:
        """标签下的资产列表（分页）。

        注意：这里返回的是资产 DTO，**不**广播 ``ViewService.assets_viewed``
        —— 缩略图的按需请求由 UI 侧在读 ``ViewService`` 时触发；标签页如果
        需要缩略图，会在拿到资产后自己调 ``ThumbnailService.request_bulk``。
        """
        return self._tag.list_assets(tag_uuid, offset, limit)
