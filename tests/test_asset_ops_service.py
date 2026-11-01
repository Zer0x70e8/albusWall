#
"""资产写用例（``AssetOpsService`` / ``AssetTagService``）的契约。

替身只实现"本服务真正调用的方法"，因此这些用例同时在守一条边界：
**服务只跟这两个仓储说话**，不直连 DB、不碰其它服务。

重点覆盖"以仓储实际返回记账"这条纪律 —— 批量写的返回值必须来自仓储，
而不是输入（并发下输入里的资产可能已经被别人删掉）。
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

from albuswall.dto.asset.events import RestoreConflictReason, SkipReason
from albuswall.dto.utils import to_uuid
from albuswall.service.asset.command import AssetOpsService, AssetTagService
from albuswall.service.errors import (
    AlbumNotFound,
    AssetNotFound,
    BatchAtomicityError,
    NotAMutableAlbum,
    NotAVisibleAsset,
)

ALBUM = uuid4()
TRASH_ALBUM = uuid4()
SYSTEM_ALBUM = uuid4()


class _Asset:
    def __init__(self, uuid: UUID, favorite: bool = False) -> None:
        self.uuid = uuid
        self.is_favorite = favorite


class _FakeAssetRepo:
    """只实现被调用的写方法；返回值刻意与"输入"不同，用来验证记账口径。"""

    def __init__(self) -> None:
        self.favorite_writes: list[tuple[Any, bool]] = []
        self.soft_deleted: list[Any] = []
        self.added: list[Any] = []
        self.removed: list[Any] = []
        self.move_calls: list[Any] = []
        self.move_exc: Optional[BaseException] = None
        #: 仓储"只成功处理了这些"（其余视为并发丢失）
        self.effective: Optional[set[UUID]] = None

    def _effective(self, uuids):
        if self.effective is None:
            return set(uuids)
        return set(uuids) & self.effective

    def set_favorite(self, uuid, value):
        self.favorite_writes.append((uuid, value))

    def soft_delete_by_uuids(self, uuids):
        self.soft_deleted.append(list(uuids))
        return self._effective(uuids)

    def memberships(self, album_uuid, uuids):
        return {to_uuid(u): False for u in uuids}

    def add_many_to_album(self, album_uuid, uuids):
        self.added.append(list(uuids))
        return self._effective(uuids)

    def remove_many_from_album(self, album_uuid, uuids):
        self.removed.append(list(uuids))
        return self._effective(uuids)

    def move_between_albums(self, src, dst, uuids):
        self.move_calls.append((src, dst, list(uuids)))
        if self.move_exc is not None:
            raise self.move_exc

        class _Outcome:
            moved = tuple(uuids)
            already_in_target: tuple = ()
            skipped: tuple = ()

        return _Outcome()

    def add_tags_to_asset(self, asset_uuid, tag_uuids):
        return len(tag_uuids)

    def remove_tags_from_asset(self, asset_uuid, tag_uuids):
        return len(tag_uuids)


class _FakeViewRepo:
    def __init__(self, states: Optional[dict[UUID, str]] = None) -> None:
        self.states = states or {}
        self.assets: dict[UUID, _Asset] = {}

    def classify_by_uuids(self, uuids):
        return {
            str(u): self.states[u] for u in uuids if u in self.states
        }

    def get_asset(self, uuid, *, include_deleted=False):
        return self.assets.get(to_uuid(uuid))

    def is_trash_album(self, uuid):
        return to_uuid(uuid) == TRASH_ALBUM

    def is_system_album(self, uuid):
        return to_uuid(uuid) == SYSTEM_ALBUM

    def album_exists(self, uuid):
        return to_uuid(uuid) in {ALBUM, TRASH_ALBUM, SYSTEM_ALBUM}

    def album_is_visible(self, uuid):
        return to_uuid(uuid) == ALBUM


class _FakeTrash:
    def __init__(self) -> None:
        self.restore_calls: list[Any] = []
        self.empty_calls = 0

    def restore_by_uuids(self, uuids):
        self.restore_calls.append(list(uuids))

        class _Result:
            restored = tuple(uuids)
            not_found: tuple = ()
            conflicts: tuple = ()

        return _Result()

    def empty_trash(self):
        self.empty_calls += 1

        class _Report:
            purged = (uuid4(),)
            thumb_failures: tuple = ()
            requested = 1
            skipped: tuple = ()
            completed = True
            error = None
            count_after = 0

        return _Report()

    def count_deleted(self):
        return 3


def _ops(
        states: Optional[dict[UUID, str]] = None,
) -> tuple[AssetOpsService, _FakeAssetRepo, _FakeViewRepo, _FakeTrash]:
    asset = _FakeAssetRepo()
    view = _FakeViewRepo(states)
    trash = _FakeTrash()
    return AssetOpsService(asset, view, trash), asset, view, trash


# =========================================================================== #
# 收藏
# =========================================================================== #

def test_set_favorite_updates_and_reports_previous():
    ops, asset, view, _ = _ops()
    uuid = uuid4()
    view.assets[uuid] = _Asset(uuid, favorite=False)

    result = ops.set_favorite(uuid, True)

    assert result.outcome == "updated"
    assert result.previous_value is False
    assert asset.favorite_writes == [(uuid, True)]


def test_set_favorite_is_noop_when_value_already_matches():
    ops, asset, view, _ = _ops()
    uuid = uuid4()
    view.assets[uuid] = _Asset(uuid, favorite=True)

    result = ops.set_favorite(uuid, True)

    assert result.outcome == "noop"
    assert asset.favorite_writes == [], "值没变就不该写库"


def test_set_favorite_missing_asset_is_not_an_error():
    ops, _asset, _view, _ = _ops()

    assert ops.set_favorite(uuid4(), True).outcome == "not_found"


# =========================================================================== #
# 软删 / 恢复
# =========================================================================== #

def test_trash_splits_visible_deleted_and_missing():
    ops, asset, _view, _ = _ops()
    visible, deleted, missing = uuid4(), uuid4(), uuid4()
    ops._view_repo.states = {
        visible: "active", deleted: "deleted",
    }

    result = ops.trash([visible, deleted, missing])

    assert result.changed == (visible,)
    assert result.already_deleted == (deleted,)
    assert result.missing == (missing,)
    assert asset.soft_deleted == [[visible]], "只对可见的调用批量软删"


def test_trash_trusts_the_repository_return_value():
    """仓储只成功了一部分时，``changed`` 必须是它报的那部分。"""
    ops, asset, _view, _ = _ops()
    a, b = uuid4(), uuid4()
    ops._view_repo.states = {a: "active", b: "active"}
    asset.effective = {a}

    result = ops.trash([a, b])

    assert result.changed == (a,)
    assert b not in result.changed


def test_restore_maps_conflicts_to_domain_enum():
    ops, _asset, _view, trash = _ops()
    uuid = uuid4()

    class _Conflict:
        asset_uuid = uuid
        reason = "path_conflict"
        detail = "a/b.png"

    class _Trash(_FakeTrash):
        def restore_by_uuids(self, uuids):
            class _Result:
                restored: tuple = ()
                not_found: tuple = ()
                conflicts = (_Conflict(),)

            return _Result()

    ops._trash_service = _Trash()
    result = ops.restore([uuid])

    assert result.conflicts[0].reason is RestoreConflictReason.PATH_CONFLICT
    assert result.conflicts[0].detail == "a/b.png"


# =========================================================================== #
# 相册归属
# =========================================================================== #

def test_add_to_album_reports_added_and_skipped():
    ops, asset, _view, _ = _ops()
    ok, gone, missing = uuid4(), uuid4(), uuid4()
    ops._view_repo.states = {ok: "active", gone: "deleted"}

    result = ops.add_to_album(ALBUM, [ok, gone, missing])

    assert result.added == (ok,)
    assert {s.asset_uuid for s in result.skipped} == {gone, missing}
    assert {s.reason for s in result.skipped} == {
        SkipReason.ASSET_NOT_VISIBLE, SkipReason.ASSET_MISSING,
    }
    assert asset.added == [[ok]]


def test_album_guards():
    ops, *_ = _ops()

    with pytest.raises(NotAMutableAlbum) as info:
        ops.add_to_album(TRASH_ALBUM, [uuid4()])
    assert info.value.reason == "trash"

    with pytest.raises(NotAMutableAlbum) as info:
        ops.add_to_album(SYSTEM_ALBUM, [uuid4()])
    assert info.value.reason == "system"

    with pytest.raises(AlbumNotFound):
        ops.add_to_album(uuid4(), [uuid4()])


def test_remove_from_album_accepts_deleted_assets():
    """已软删资产也要能从相册里清掉（否则回收站里的资产仍占着相册）。"""
    ops, asset, _view, _ = _ops()
    deleted = uuid4()
    ops._view_repo.states = {deleted: "deleted"}

    result = ops.remove_from_album(ALBUM, [deleted])

    assert result.skipped == (), "存在但不是可见 ≠ 不存在"
    assert result.not_present == (deleted,), "不在相册里 → not_present"
    assert asset.removed == [], "本来就不在相册，不该调删除"


def test_move_is_atomic_and_translates_failure():
    ops, asset, _view, _ = _ops()
    asset.move_exc = RuntimeError("deadlock")

    with pytest.raises(BatchAtomicityError) as info:
        ops.move_between_albums(ALBUM, ALBUM, [uuid4()])
    assert isinstance(info.value.cause, RuntimeError)


def test_move_rejects_virtual_albums_even_when_src_equals_dst():
    ops, asset, _view, _ = _ops()

    with pytest.raises(NotAMutableAlbum):
        ops.move_between_albums(TRASH_ALBUM, TRASH_ALBUM, [uuid4()])
    assert asset.move_calls == []


def test_copy_is_alias_of_add():
    ops, _asset, _view, _ = _ops()
    uuid = uuid4()
    ops._view_repo.states = {uuid: "active"}

    assert ops.copy_into_album(ALBUM, [uuid]).added == (uuid,)


# =========================================================================== #
# 回收站 / 分类
# =========================================================================== #

def test_empty_trash_delegates_and_backfills_fields():
    ops, _asset, _view, trash = _ops()

    report = ops.empty_trash()

    assert trash.empty_calls == 1
    assert report.requested == 1
    assert report.count_after == 0
    assert ops.count_deleted() == 3


def test_unknown_visibility_state_is_treated_as_missing():
    ops, _asset, _view, _ = _ops()
    uuid = uuid4()
    ops._view_repo.states = {uuid: "zombie"}

    classified = ops.classify_assets([uuid])

    assert classified.missing == (uuid,)


def test_ensure_asset_visible_raises_domain_errors():
    ops, *_ = _ops()
    uuid = uuid4()

    with pytest.raises(AssetNotFound):
        ops.ensure_asset_visible(uuid)

    ops._view_repo.states = {uuid: "deleted"}
    with pytest.raises(NotAVisibleAsset):
        ops.ensure_asset_visible(uuid)


# =========================================================================== #
# 资产 × 标签
# =========================================================================== #

class _FakeTagRepo:
    def __init__(self, known: set[UUID]) -> None:
        self.known = known

    def exists(self, uuid, include_deleted=False):
        return to_uuid(uuid) in self.known

    def get_tag_uuids_of_asset(self, asset_uuid):
        return []

    def get_tags_of_asset(self, asset_uuid):
        return []

    def count_assets(self, tag_uuid):
        return 0

    def count_assets_bulk(self, tag_uuids):
        return {str(u): 0 for u in tag_uuids}

    def list_assets(self, tag_uuid, offset=0, limit=100):
        return []


def test_asset_tag_add_skips_unknown_tags():
    asset = _FakeAssetRepo()
    view = _FakeViewRepo()
    uuid = uuid4()
    view.assets[uuid] = _Asset(uuid)
    known, unknown = uuid4(), uuid4()
    service = AssetTagService(asset, _FakeTagRepo({known}), view)

    result = service.add(uuid, [known, unknown, known])

    assert result.added == 1
    assert result.existing == 0
    assert result.skipped_uuids == (unknown,)


def test_asset_tag_add_requires_visible_asset():
    asset = _FakeAssetRepo()
    service = AssetTagService(asset, _FakeTagRepo(set()), _FakeViewRepo())

    with pytest.raises(AssetNotFound):
        service.add(uuid4(), [uuid4()])
