#
"""资产 × 标签绑定业务服务。

与 TagService 的分工：
    · TagService  管标签自身的生命周期（create/rename/move/delete）；
    · AssetTagService 管「哪个资产挂了哪些标签」（增删 / 全量设置 / 查询）。

消歧义：
    · repo.add_tags_to_asset 的 0 行 = 已存在 OR 资产/标签不可见；
    · 本 service 先查资产、过滤标签，把 0 行翻译成结构化结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from albuswall.dto.album import AssetDTO
from albuswall.dto.tag import Tag
from albuswall.repositories.asset import AssetRepository
from albuswall.repositories.tag import TagRepository
from albuswall.repositories.view import ViewRepository


# ============================== 错误 ==============================


class AssetTagError(Exception):
    """Asset × Tag 绑定错误基类。"""


class AssetNotFound(AssetTagError):
    def __init__(self, uuid: str):
        super().__init__(f"asset not found: {uuid}")
        self.uuid = uuid


# ============================== 结果 DTO ==============================


@dataclass(frozen=True, slots=True)
class AddTagsResult:
    added: int
    existing: int  # 已存在绑定（幂等跳过）
    skipped_uuids: tuple[str, ...]  # 不存在或软删的标签


@dataclass(frozen=True, slots=True)
class SetTagsResult:
    added: int
    removed: int
    existing: int
    skipped_uuids: tuple[str, ...]


# ============================== Service ==============================


class AssetTagService:
    """资产 × 标签的绑定编排。

    资产存在性用 ViewRepository.get_asset 判断（repo 层没有 exists 方法）；
    标签存在性用 TagRepository.exists（默认过滤软删）。
    """

    def __init__(
            self,
            asset_repo: AssetRepository,
            tag_repo: TagRepository,
            view_repo: ViewRepository,
    ):
        self._asset = asset_repo
        self._tag = tag_repo
        self._view = view_repo

    # ---------------------------------------------------------------- #
    # 写
    # ---------------------------------------------------------------- #

    def add(
            self,
            asset_uuid: str,
            tag_uuids: Sequence[str],
    ) -> AddTagsResult:
        """给资产打标签。

        前置：资产必须可见；不存在的标签被静默跳过并在结果里列出。
        返回：实际新增 / 已存在 / 被跳过的数量与 uuid。
        """
        if self._view.get_asset(asset_uuid, include_deleted=False) is None:
            raise AssetNotFound(asset_uuid)

        unique = list(dict.fromkeys(tag_uuids))
        valid: list[str] = []
        skipped: list[str] = []
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
            asset_uuid: str,
            tag_uuids: Sequence[str],
    ) -> int:
        """移除绑定；软删标签的绑定保留（与 repo 语义一致）。"""
        return self._asset.remove_tags_from_asset(asset_uuid, tag_uuids)

    def set(
            self,
            asset_uuid: str,
            tag_uuids: Sequence[str],
    ) -> SetTagsResult:
        """全量设置：按当前绑定 diff 出新增和移除。

        非原子（add / remove 各自带事务），但整体幂等：
        中途失败时重试会收敛到目标状态。
        """
        if self._view.get_asset(asset_uuid, include_deleted=False) is None:
            raise AssetNotFound(asset_uuid)

        current = set(self._tag.get_tag_uuids_of_asset(asset_uuid))
        target = set(tag_uuids)
        to_add = [u for u in dict.fromkeys(tag_uuids) if u not in current]
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

    def list_of_asset(self, asset_uuid: str) -> list[Tag]:
        return self._tag.get_tags_of_asset(asset_uuid)

    def count_for_tag(self, tag_uuid: str) -> int:
        return self._tag.count_assets(tag_uuid)

    def count_for_tags(self, tag_uuids: Sequence[str]) -> dict[str, int]:
        return self._tag.count_assets_bulk(tag_uuids)

    def list_assets_of_tag(
            self,
            tag_uuid: str,
            offset: int = 0,
            limit: int = 100,
    ) -> list[AssetDTO]:
        return self._tag.list_assets(tag_uuid, offset, limit)
