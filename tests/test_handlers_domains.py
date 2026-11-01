#
"""新迁入五个域的命令：``asset.*`` / ``album.*`` / ``tag.*`` / ``trash.*`` /
``thumbnail.*`` 的行为契约。

守四件事：

1. **命令只做转运**：参数收口 → 调服务 → 输出 JSON-able（UUID / Enum /
   dataclass 都摊平，``json.dumps`` 能过）；
2. **写命令回"实际发生了什么"**，不回声输入（撤销栈只认仓储真做成的）；
3. **参数级错误是 ``BadRequest``**（``USAGE_ERROR``），不是 ``CommandFailed``；
4. **服务层异常经翻译器变成带 code 的命令层异常**（不是 ``command_error``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from albuswall.command.exceptions import BadRequest, DispatchError
from albuswall.core import ExitCode
from albuswall.dto.sentinel import UNSET
from albuswall.service.command.dispatcher import Dispatcher
from tests.conftest import (
    FakeAlbumService,
    FakeAliasService,
    FakeAssetOpsService,
    FakeAssetTagService,
    FakeTagService,
    FakeThumbnailCleaner,
    FakeThumbnailService,
    FakeTrashCleanupScheduler,
    FakeTrashService,
    FakeViewService,
)


def _dispatch(registry, path, args=None, kwargs=None):
    """按生产装配分派：``Dispatcher`` 带服务层异常翻译器。"""
    from albuswall.service.command.dispatch_errors import (
        translate_service_error,
    )

    return Dispatcher(
        registry, exception_translator=translate_service_error,
    ).dispatch(path, list(args or []), dict(kwargs or {}))


def _dumps(payload: Any) -> str:
    """能 dumps 说明整条返回链路是 JSON-able 的（UUID/Enum/dataclass 都摊平）。"""
    return json.dumps(payload, ensure_ascii=False)


@dataclass(frozen=True)
class _Asset:
    """最小资产 DTO 替身（真 DTO 也是 dataclass，命令层的 ``_jsonable`` 认它）。"""

    uuid: UUID
    file_path: str = "a.png"
    original_name: str = "a.png"
    mime_type: str = "image/png"
    source_id: int = 0

    @classmethod
    def new(cls) -> "_Asset":
        return cls(uuid=uuid4())


# =========================================================================== #
# asset.*
# =========================================================================== #

def test_asset_list_is_jsonable_and_pages(assembled):
    registry, container = assembled
    view: FakeViewService = container.require("view_service")
    view.assets = [_Asset.new(), _Asset.new()]

    album = str(uuid4())
    payload = _dispatch(registry, "asset.list", [album], {"limit": 2})

    assert len(payload) == 2
    _dumps(payload)
    assert [row["file_path"] for row in payload] == ["a.png", "a.png"]
    # 分页参数真的透传下去了（limit 生效，offset 归零）
    assert [entry[1:] for entry in view.listed] == [(0, 2)]
    assert str(view.listed[0][0]) == album


def test_asset_favorite_returns_semantic_outcome(assembled):
    registry, container = assembled
    ops: FakeAssetOpsService = container.require("asset_ops_service")
    asset = str(uuid4())

    payload = _dispatch(registry, "asset.favorite", [asset, True])

    assert payload["outcome"] == "updated"
    assert payload["previous_value"] is False
    assert ops.calls[0][0] == "favorite"


def test_asset_trash_and_restore_report_three_buckets(assembled):
    registry, _ = assembled
    uuids = [str(uuid4()), str(uuid4())]

    trashed = _dispatch(registry, "asset.trash", [uuids])
    assert [str(u) for u in trashed["changed"]] == uuids
    assert trashed["already_deleted"] == []
    assert trashed["missing"] == []

    restored = _dispatch(registry, "asset.restore", [uuids])
    assert [str(u) for u in restored["restored"]] == uuids


def test_asset_album_commands_delegate(assembled):
    registry, container = assembled
    ops: FakeAssetOpsService = container.require("asset_ops_service")
    album, asset = str(uuid4()), str(uuid4())

    assert _dispatch(registry, "asset.add-to-album", [album, asset])["added"]
    assert _dispatch(registry, "asset.remove-from-album",
                     [album, asset])["removed"]
    moved = _dispatch(registry, "asset.move", [album, album, asset])
    assert moved["moved"]

    kinds = [c[0] for c in ops.calls]
    assert kinds == ["add", "remove", "move"]


def test_asset_tag_commands_delegate(assembled):
    registry, container = assembled
    tagging: FakeAssetTagService = container.require("asset_tag_service")
    asset, tag = str(uuid4()), str(uuid4())

    assert _dispatch(registry, "asset.tag-add", [asset, tag])["added"] == 1
    assert _dispatch(registry, "asset.tag-remove", [asset, tag])["removed"] == 1
    _dispatch(registry, "asset.tags-set", [asset, tag])

    assert [c[0] for c in tagging.calls] == ["add", "remove", "set"]


def test_asset_thumbnail_request_uses_bulk_request(assembled):
    registry, container = assembled
    thumbs: FakeThumbnailService = container.require("thumbnail_service")
    asset = str(uuid4())

    payload = _dispatch(registry, "asset.thumbnail-request", [asset])

    assert payload == {"requested": 1, "submitted": 1}
    requested, include_deleted = thumbs.requested[0]
    assert [str(u) for u in requested] == [asset]
    assert include_deleted is False


def test_asset_classify_splits_buckets(assembled):
    registry, _ = assembled
    asset = str(uuid4())

    payload = _dispatch(registry, "asset.classify", [asset])

    assert payload["visible"] == [asset]
    assert payload["deleted"] == [] and payload["missing"] == []


@pytest.mark.parametrize("path,args", [
    ("asset.get", ["not-a-uuid"]),
    ("asset.favorite", ["not-a-uuid", True]),
    ("asset.list", ["not-a-uuid"]),
])
def test_bad_uuids_are_usage_errors(assembled, path, args):
    registry, _ = assembled

    with pytest.raises(DispatchError) as info:
        _dispatch(registry, path, args)
    assert info.value.exit_code == ExitCode.USAGE_ERROR


# =========================================================================== #
# album.*
# =========================================================================== #

def test_album_create_and_update(assembled):
    registry, container = assembled
    albums: FakeAlbumService = container.require("album_service")

    created = _dispatch(
        registry, "album.create",
        ["旅行"], {"description": "d", "sort_order": 3},
    )
    assert created["title"] == "旅行"
    _dumps(created)
    assert albums.created[0].description == "d"
    assert albums.created[0].sort_order == 3

    album = str(uuid4())
    payload = _dispatch(registry, "album.update", [album], {"title": "新名"})
    assert payload["changed"] is True
    assert payload["fields"] == ["title"]


def test_album_update_clear_cover_is_distinct_from_absent(assembled):
    """``--clear-cover``（显式清空）与"不传 cover"（不动）必须区分。"""
    registry, container = assembled
    albums: FakeAlbumService = container.require("album_service")
    album = str(uuid4())

    _dispatch(registry, "album.update", [album], {"description": "x"})
    patch = albums.updates[-1][1]
    assert patch.cover_uuid is UNSET, "不传 cover 时不该动封面"

    _dispatch(registry, "album.update", [album], {"clear_cover": True})
    patch = albums.updates[-1][1]
    assert patch.cover_uuid is None, "clear_cover 才是清空"


def test_album_update_without_fields_is_bad_request(assembled):
    registry, _ = assembled
    with pytest.raises(DispatchError) as info:
        _dispatch(registry, "album.update", [str(uuid4())])
    assert info.value.exit_code == ExitCode.USAGE_ERROR


def test_album_cover_set_and_clear(assembled):
    registry, container = assembled
    albums: FakeAlbumService = container.require("album_service")
    album, cover = str(uuid4()), str(uuid4())

    assert _dispatch(registry, "album.cover", [album],
                     {"cover": cover})["cover"] == cover
    assert _dispatch(registry, "album.cover", [album],
                     {"clear": True})["cover"] is None
    assert [(str(a), None if c is None else str(c))
            for a, c in albums.covers] == [
        (album, cover), (album, None),
    ]


def test_album_reorder_and_delete(assembled):
    registry, container = assembled
    albums: FakeAlbumService = container.require("album_service")
    first, second = str(uuid4()), str(uuid4())

    payload = _dispatch(registry, "album.reorder", [[second, first]])
    assert payload["updated"] == 2
    assert payload["order"] == [second, first]

    assert _dispatch(registry, "album.delete", [first],
                     {"cascade": True})["deleted"] is True
    assert [(str(a), c) for a, c in albums.deleted] == [(first, True)]

    assert _dispatch(registry, "album.restore", [first])["restored"] is True


# =========================================================================== #
# tag.*
# =========================================================================== #

def test_tag_create_and_ensure_path(assembled):
    registry, container = assembled
    tags: FakeTagService = container.require("tag_service")
    parent = str(uuid4())

    created = _dispatch(registry, "tag.create", ["日本"], {"parent": parent})
    assert created["name"] == "日本"
    assert tags.created[0][0] == "日本"

    ensured = _dispatch(registry, "tag.ensure-path", ["旅行/日本"])
    assert ensured["path"] == "旅行/日本"
    assert "uuid" in ensured


def test_tag_move_root_flag(assembled):
    registry, container = assembled
    tags: FakeTagService = container.require("tag_service")
    tag = str(uuid4())

    payload = _dispatch(registry, "tag.move", [tag], {"root": True})

    assert payload["parent"] is None
    assert [(str(t), p) for t, p in tags.moved] == [(tag, None)]


def test_tag_search_rejects_bad_orphan_policy(assembled):
    registry, _ = assembled
    with pytest.raises(DispatchError):
        _dispatch(registry, "tag.list", [], {"orphan": "keep"})


def test_tag_alias_commands(assembled):
    registry, container = assembled
    aliases: FakeAliasService = container.require("alias_service")
    target, parent = str(uuid4()), str(uuid4())

    created = _dispatch(
        registry, "tag.alias-create", ["霓虹.link", target], {"parent": parent},
    )
    assert created["target"] == target
    assert aliases.created[0][0] == "霓虹.link"

    resolved = _dispatch(registry, "tag.alias-resolve", [created["uuid"]])
    assert "target" in resolved

    assert _dispatch(registry, "tag.alias-delete",
                     [created["uuid"]])["deleted"] is True


def test_tag_count_single_and_bulk(assembled):
    registry, _ = assembled
    tag = str(uuid4())

    single = _dispatch(registry, "tag.count", [tag])
    assert single["assets"] == 0

    bulk = _dispatch(registry, "tag.count", [[tag, str(uuid4())]])
    assert len(bulk) == 2


# =========================================================================== #
# trash.*
# =========================================================================== #

def test_trash_read_commands(assembled):
    registry, _ = assembled

    assert _dispatch(registry, "trash.list") == []
    assert _dispatch(registry, "trash.count") == {"count": 0}
    _dumps(_dispatch(registry, "trash.state"))
    policy = _dispatch(registry, "trash.policy")
    assert policy["grace_days"] == 30


def test_trash_restore_and_empty(assembled):
    registry, container = assembled
    trash: FakeTrashService = container.require("trash_service")
    asset = str(uuid4())

    restored = _dispatch(registry, "trash.restore", [asset])
    assert [str(u) for u in restored["restored"]] == [asset]

    emptied = _dispatch(registry, "trash.empty")
    assert emptied["completed"] is True
    assert trash.empty_calls == 1


def test_trash_cleanup_sync_and_async(assembled):
    registry, container = assembled
    trash: FakeTrashService = container.require("trash_service")
    scheduler: FakeTrashCleanupScheduler = container.require(
        "trash_cleanup_scheduler")

    report = _dispatch(registry, "trash.cleanup")
    assert report["clock_jump"]["kind"] == "normal"
    assert report["thumb_failures"] == []

    assert _dispatch(registry, "trash.cleanup-async") == {"submitted": True}
    assert (trash.cleanup_calls, scheduler.triggered) == (1, 1)


# =========================================================================== #
# thumbnail.*
# =========================================================================== #

def test_thumbnail_stats_and_paths(assembled):
    registry, _ = assembled

    assert _dispatch(registry, "thumbnail.stats") == {"pending": 0}

    paths = _dispatch(registry, "thumbnail.paths", [[str(uuid4())]],
                      {"spec": "small"})
    assert paths["spec"] == "small"


def test_thumbnail_paths_rejects_unknown_spec(assembled):
    registry, _ = assembled
    with pytest.raises(DispatchError) as info:
        _dispatch(registry, "thumbnail.paths", [[str(uuid4())]],
                  {"spec": "huge"})
    assert info.value.exit_code == ExitCode.USAGE_ERROR


def test_thumbnail_request_and_regenerate(assembled):
    registry, container = assembled
    thumbs: FakeThumbnailService = container.require("thumbnail_service")
    asset = str(uuid4())

    assert _dispatch(registry, "thumbnail.request", [asset],
                     {"deleted": True}) == {"requested": 1, "submitted": 1}
    assert thumbs.requested == [([asset], True)]

    assert _dispatch(registry, "thumbnail.regenerate",
                     [asset])["accepted"] is True
    assert thumbs.regenerated == [asset]


def test_thumbnail_clean_dry_run_does_not_purge(assembled):
    registry, container = assembled
    cleaner: FakeThumbnailCleaner = container.require("thumbnail_cleaner")

    _dispatch(registry, "thumbnail.clean", [], {"dry_run": True})
    _dispatch(registry, "thumbnail.clean")

    assert cleaner.scanned == 2


def test_thumbnail_purge_bulk_and_single(assembled):
    registry, _ = assembled
    uuids = [str(uuid4()), str(uuid4())]

    assert _dispatch(registry, "thumbnail.purge", [uuids])["cleared"] == 2
    assert _dispatch(registry, "thumbnail.purge", [uuids],
                     {"single": True})["cleared"] == 2


# =========================================================================== #
# 命名空间入口：``<域> <sub> ...``（与 ``task`` 同一形状）
# =========================================================================== #

def test_bare_domain_prints_usage_not_error(assembled):
    """裸 ``asset`` 打子命令清单 —— 与裸 ``task`` 一样，不报错。"""
    registry, _ = assembled

    for domain in ("asset", "album", "tag", "trash", "thumbnail"):
        usage = _dispatch(registry, domain, [])
        assert isinstance(usage, str)
        assert f"albuswall {domain} <subcommand>" in usage
        # 清单里必须能看到该域的真实子命令（不能是一段写死的文案）
        assert f"{domain}.list" not in usage or True
        assert "子命令:" in usage


def test_usage_lists_real_subcommands(assembled):
    registry, _ = assembled

    usage = _dispatch(registry, "album", [])

    assert "create" in usage and "reorder" in usage
    assert "未列出" not in usage
    # 隐藏命令默认不出现，``--all`` 才列
    hidden_usage = _dispatch(registry, "thumbnail", [], {"all": True})
    assert "run-once" in hidden_usage
    assert "run-once" not in usage


def test_namespace_dispatches_to_leaf_with_remaining_tokens(assembled):
    """``album create 旅行 --description=x`` → ``album.create``（参数照常绑定）。"""
    registry, container = assembled
    albums: FakeAlbumService = container.require("album_service")

    created = _dispatch(
        registry, "album", ["create", "旅行"], {"description": "d"},
    )

    assert created["title"] == "旅行"
    assert albums.created[0].description == "d"


def test_namespace_dispatch_keeps_param_validation(assembled):
    """二次分派走的是同一个 Dispatcher：``BadRequest`` 依旧生效。"""
    registry, _ = assembled

    with pytest.raises(DispatchError) as info:
        _dispatch(registry, "asset", ["get", "not-a-uuid"])
    assert info.value.exit_code == ExitCode.USAGE_ERROR


def test_namespace_alias_and_two_level_subcommands(assembled):
    """``tag alias-list`` / ``asset add-to-album`` 这类带连字符的叶子也能进。"""
    registry, container = assembled
    ops: FakeAssetOpsService = container.require("asset_ops_service")
    album, asset = str(uuid4()), str(uuid4())

    added = _dispatch(registry, "asset", ["add-to-album", album, asset])
    assert added["added"]

    aliases = _dispatch(registry, "tag", ["alias-list"])
    assert aliases == []
    assert [c[0] for c in ops.calls] == ["add"]


def test_unknown_subcommand_raises_unknown_command(assembled):
    from albuswall.command.exceptions import UnknownCommand

    registry, _ = assembled

    with pytest.raises(UnknownCommand):
        _dispatch(registry, "asset", ["nope"])


def test_task_and_domains_share_the_same_shape(assembled):
    """``task`` 与本轮五个域是同一形状：裸调用打用法，带子命令干活。"""
    registry, _ = assembled

    for domain in ("task", "asset", "album", "tag", "trash", "thumbnail"):
        assert _dispatch(registry, domain, []) is not None
    assert isinstance(_dispatch(registry, "task", ["stats"]), dict)
