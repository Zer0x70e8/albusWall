#
"""相簿 / 标签别名：**真 sqlite** 下的写路径。

为什么用真 DB：这两块是本轮**新写**的 SQL（``repositories/album.py`` 与
``TagRepository`` 的别名方法），替身测不出"列名写错""JOIN 条件写反"这类
错误。真库很便宜（内存 / 临时文件 + 现成的 schema 脚本），值得。

覆盖：
* ``albums``：建 / 改名 / 设封面（含"资产不可见 → 不写封面"）/ 排序 /
  软删（成员保留）/ 恢复 / 守卫（系统相册、回收站相册、不存在）；
* ``tag_aliases``：建 / 解析 / 同层重名 / 目标不存在 / **禁环**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from albuswall.common.constants.albums import (
    VIRTUAL_ALBUM_ALL_UUID,
    VIRTUAL_ALBUM_TRASH_UUID,
)
from albuswall.dto.album.command import AlbumCreate, AlbumReorder, AlbumUpdate
from albuswall.repositories.album import AlbumRepository
from albuswall.repositories.tag import TagRepository
from albuswall.repositories.view import ViewRepository
from albuswall.service.album import AlbumService
from albuswall.service.errors import AlbumNotFound, NotAMutableAlbum
from albuswall.service.tag import AliasService, TagService
from albuswall.service.tag.aliases import AliasCycle
from albuswall.service.tag.errors import TagNameConflict, TagNotFound

_SCHEMA = Path(__file__).resolve().parents[1] / (
    "src/albuswall/resources/sql/media_library_schema.sql"
)


class _Db:
    """最小 DB 门面：一条连接 + 建表（仓库层只要求 ``connect()``）。"""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        self._conn.commit()

    def connect(self) -> sqlite3.Connection:
        return self._conn

    # 测试里插数据用
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur


@pytest.fixture
def db() -> _Db:
    return _Db()


@pytest.fixture
def album_service(db: _Db) -> AlbumService:
    return AlbumService(AlbumRepository(db), ViewRepository(db))


@pytest.fixture
def tag_tools(db: _Db) -> tuple[TagService, AliasService, TagRepository]:
    repo = TagRepository(db)
    return TagService(repo), AliasService(repo), repo


def _insert_asset(db: _Db, *, is_deleted: int = 0) -> str:
    asset_uuid = str(uuid4())
    db.execute(
        "INSERT INTO assets (uuid, file_path, source_id, original_name,"
        " mime_type, file_hash, is_deleted) VALUES (?,?,?,?,?,?,?)",
        (asset_uuid, f"{asset_uuid}.png", 0, "a.png", "image/png",
         asset_uuid, is_deleted),
    )
    return asset_uuid


# =========================================================================== #
# 相簿
# =========================================================================== #

def test_create_reads_back_through_view(album_service: AlbumService):
    album = album_service.create(AlbumCreate(title="  旅行  ", description="d"))

    assert album.title == "旅行", "标题两端空白被裁掉"
    assert album.description == "d"
    assert album_service.exists(album.uuid) is True


def test_rename_and_patch(album_service: AlbumService):
    album = album_service.create(AlbumCreate(title="旧名"))

    assert album_service.rename(album.uuid, "新名") is True
    assert album_service.update(
        album.uuid, AlbumUpdate(description="备注", sort_order=7)
    ) is True
    after = album_service.get(album.uuid)
    assert after.title == "新名"
    assert after.description == "备注"


def test_empty_patch_is_a_noop(album_service: AlbumService):
    album = album_service.create(AlbumCreate(title="x"))
    assert album_service.update(album.uuid, AlbumUpdate()) is False


def test_set_cover_uses_visible_asset(db: _Db, album_service: AlbumService):
    album = album_service.create(AlbumCreate(title="a"))
    visible = _insert_asset(db)

    assert album_service.set_cover(album.uuid, visible) is True
    assert str(album_service.get(album.uuid).cover) == visible

    # 清空
    assert album_service.set_cover(album.uuid, None) is True
    assert album_service.get(album.uuid).cover is None


def test_set_cover_ignores_invisible_asset(db: _Db, album_service: AlbumService):
    """封面指向不可见资产时不报错，但也不会写进去（子查询给 NULL）。"""
    album = album_service.create(AlbumCreate(title="a"))
    deleted = _insert_asset(db, is_deleted=1)

    assert album_service.set_cover(album.uuid, deleted) is True

    assert album_service.get(album.uuid).cover is None


def test_reorder_rewrites_sort_order(album_service: AlbumService, db: _Db):
    a = album_service.create(AlbumCreate(title="a", sort_order=9))
    b = album_service.create(AlbumCreate(title="b", sort_order=8))

    assert album_service.reorder(AlbumReorder([b.uuid, a.uuid])) == 2

    rows = db.connect().execute(
        "SELECT uuid, sort_order FROM albums ORDER BY sort_order"
    ).fetchall()
    assert [r["uuid"] for r in rows] == [str(b.uuid), str(a.uuid)]
    assert [r["sort_order"] for r in rows] == [0, 1]


def test_soft_delete_keeps_members_and_restore_brings_it_back(
        db: _Db, album_service: AlbumService,
):
    album = album_service.create(AlbumCreate(title="a"))
    asset = _insert_asset(db)
    db.execute(
        "INSERT INTO album_assets (album_id, asset_id)"
        " SELECT a.id, s.id FROM albums AS a, assets AS s"
        "  WHERE a.uuid = ? AND s.uuid = ?",
        (str(album.uuid), asset),
    )

    assert album_service.delete(album.uuid) is True
    assert album_service.exists(album.uuid) is False
    assert album_service.count_members(album.uuid) == 1, "成员行必须保留"

    assert album_service.restore(album.uuid) is True
    assert album_service.exists(album.uuid) is True


def test_cascade_delete_drops_members(db: _Db, album_service: AlbumService):
    album = album_service.create(AlbumCreate(title="a"))
    asset = _insert_asset(db)
    db.execute(
        "INSERT INTO album_assets (album_id, asset_id)"
        " SELECT a.id, s.id FROM albums AS a, assets AS s"
        "  WHERE a.uuid = ? AND s.uuid = ?",
        (str(album.uuid), asset),
    )

    assert album_service.delete(album.uuid, cascade=True) is True
    assert album_service.count_members(album.uuid) == 0


@pytest.mark.parametrize("virtual", [
    VIRTUAL_ALBUM_TRASH_UUID, VIRTUAL_ALBUM_ALL_UUID,
])
def test_virtual_albums_are_not_mutable(album_service: AlbumService, virtual):
    with pytest.raises(NotAMutableAlbum) as info:
        album_service.delete(virtual)
    assert info.value.reason in ("trash", "system")


def test_missing_album_raises(album_service: AlbumService):
    with pytest.raises(AlbumNotFound):
        album_service.rename(uuid4(), "x")


# =========================================================================== #
# 相簿成员：批量写返回"真正变成功的 uuid"（真库）
# =========================================================================== #

def _insert_album_row(db: _Db, title: str = "album") -> str:
    album_uuid = str(uuid4())
    db.execute(
        "INSERT INTO albums (uuid, title) VALUES (?, ?)",
        (album_uuid, title),
    )
    return album_uuid


def test_add_many_to_album_returns_only_actually_added(db: _Db):
    """批量加入：幂等跳过已存在的，返回值只含**真正新增**的 uuid。"""
    from albuswall.repositories.asset import AssetRepository

    album = _insert_album_row(db)
    already = _insert_asset(db)
    fresh = _insert_asset(db)
    repo = AssetRepository(db)

    assert repo.add_many_to_album(album, [already]) == {UUID(already)}

    # 再要一次（already 已在、fresh 是新的）：只报 fresh
    added = repo.add_many_to_album(album, [already, fresh])

    assert added == {UUID(fresh)}


def test_add_many_to_album_skips_invisible_assets(db: _Db):
    from albuswall.repositories.asset import AssetRepository

    album = _insert_album_row(db)
    gone = _insert_asset(db, is_deleted=1)
    live = _insert_asset(db)
    repo = AssetRepository(db)

    added = repo.add_many_to_album(album, [gone, live])

    assert added == {UUID(live)}, "不可见资产不能成为成员"


def test_remove_many_from_album_returns_only_actual_removals(db: _Db):
    from albuswall.repositories.asset import AssetRepository

    album = _insert_album_row(db)
    member = _insert_asset(db)
    outsider = _insert_asset(db)
    repo = AssetRepository(db)
    repo.add_many_to_album(album, [member])

    removed = repo.remove_many_from_album(album, [member, outsider])

    assert removed == {UUID(member)}, "本来就不在相册的不该出现在 removed 里"


def test_album_membership_through_service(db: _Db):
    """服务层端到端：加入 → 计数 → 移出（走真实 SQL）。"""
    album_service = AlbumService(AlbumRepository(db), ViewRepository(db))
    from albuswall.repositories.asset import AssetRepository
    from albuswall.service.asset.command import AssetOpsService

    album_row = _insert_album_row(db)
    album = UUID(album_row)
    a1, a2 = _insert_asset(db), _insert_asset(db)
    ops = AssetOpsService(
        AssetRepository(db), ViewRepository(db), trash_service=object(),
    )

    result = ops.add_to_album(album, [UUID(a1), UUID(a2)])
    assert {str(u) for u in result.added} == {a1, a2}
    assert album_service.count_members(album) == 2

    removed = ops.remove_from_album(album, [UUID(a1)])
    assert {str(u) for u in removed.removed} == {a1}
    assert album_service.count_members(album) == 1


# =========================================================================== #
# 标签 + 别名
# =========================================================================== #

def test_tag_create_and_alias_resolve(tag_tools):
    tags, aliases, _repo = tag_tools
    target = tags.ensure_path("旅行/日本")

    alias_uuid = aliases.create("霓虹.link", target)

    assert aliases.resolve(alias_uuid) == target
    assert str(aliases.get(alias_uuid).target_uuid) == str(target)


def test_alias_name_conflict_in_same_level(tag_tools):
    tags, aliases, _repo = tag_tools
    target = tags.ensure_path("旅行")
    aliases.create("x.link", target)

    with pytest.raises(TagNameConflict):
        aliases.create("x.link", target)


def test_alias_target_must_exist(tag_tools):
    tags, aliases, _repo = tag_tools
    with pytest.raises(TagNotFound):
        aliases.create("x.link", uuid4())


def test_alias_pointing_to_its_own_ancestor_is_refused(tag_tools):
    """别名指向自己所在层的祖先 = 向上短路，必须拒绝（见 _guard_cycle）。"""
    tags, aliases, _repo = tag_tools
    parent = tags.ensure_path("旅行")
    child = tags.ensure_path("旅行/日本")

    with pytest.raises(AliasCycle):
        aliases.create("回环.link", parent, parent_uuid=child)


def test_alias_pointing_sideways_is_allowed(tag_tools):
    """指向旁支（不是祖先）是别名的正常用法。"""
    tags, aliases, _repo = tag_tools
    tags.ensure_path("旅行")
    other = tags.ensure_path("风景")
    child = tags.ensure_path("旅行/日本")

    alias_uuid = aliases.create("同类.link", other, parent_uuid=child)

    assert aliases.resolve(alias_uuid) == other


def test_alias_delete_is_idempotent(tag_tools):
    tags, aliases, _repo = tag_tools
    target = tags.ensure_path("旅行")
    alias_uuid = aliases.create("x.link", target)

    assert aliases.delete(alias_uuid) is True
    assert aliases.delete(alias_uuid) is False


def test_alias_does_not_hold_assets(tag_tools, db: _Db):
    """别名只是解析跳转：``asset_tags`` 里永远只有规范标签 uuid。"""
    tags, aliases, _repo = tag_tools
    target = tags.ensure_path("旅行")
    alias_uuid = aliases.create("x.link", target)
    asset = _insert_asset(db)

    db.execute(
        "INSERT INTO asset_tags (asset_id, tag_uuid)"
        " SELECT s.id, ? FROM assets AS s WHERE s.uuid = ?",
        (str(target), asset),
    )
    bound = {r["tag_uuid"] for r in db.connect().execute(
        "SELECT tag_uuid FROM asset_tags").fetchall()}

    assert str(target) in bound
    assert str(alias_uuid) not in bound
