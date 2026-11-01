#
"""``ViewService.get_cover_full_path`` 的封面优先级（含兜底）。

回归背景：相册没有显式 ``cover_asset_id`` 时，旧实现直接返回 ``None``，
于是「往空相册里移入资产」后封面一直空白。契约见
``service/view/service.py``：显式封面 → 相册内排序最新的可见资产 → ``None``。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from albuswall.common.constants.albums import VIRTUAL_ALBUM_TRASH_UUID
from albuswall.dto.album import Asset
from albuswall.service.view import ViewService


def _asset(name: str = "a.jpg") -> Asset:
    return Asset(
        uuid=uuid4(),
        file_path=name,
        original_name=name,
        mime_type="image/jpeg",
        file_hash="hash",
        source_id=1,
    )


class FakeRepo:
    """最小的 ``ViewRepository`` 替身（只实现封面 / 路径链路）。"""

    def __init__(self) -> None:
        self.explicit: Asset | None = None
        self.members: list[Asset] = []
        self.scope_cover: Asset | None = None
        self.list_calls: list[tuple[str, int, int]] = []

    def get_album_cover(self, album_uuid):
        return self.explicit

    def get_cover_asset_by_scope(self, scope):
        return self.scope_cover

    def list_assets(self, album_uuid, offset=0, limit=100):
        self.list_calls.append((str(album_uuid), offset, limit))
        return self.members[:limit]

    def get_asset_path(self, asset_uuid, *, include_deleted=False):
        return f"/photos/{asset_uuid}.jpg"


@pytest.fixture
def repo():
    return FakeRepo()


def test_explicit_cover_wins(repo):
    service = ViewService(repo)
    cover = _asset()
    repo.explicit = cover
    repo.members = [_asset()]

    assert service.get_cover_full_path(uuid4()) == f"/photos/{cover.uuid}.jpg"
    assert repo.list_calls == []  # 有显式封面就不查成员页


def test_falls_back_to_latest_album_asset(repo):
    service = ViewService(repo)
    newest = _asset()
    repo.members = [newest, _asset()]

    path = service.get_cover_full_path(uuid4())

    assert path == f"/photos/{newest.uuid}.jpg"
    # 只取一页第一条（排序契约 = taken_at DESC, id DESC）
    assert repo.list_calls and repo.list_calls[0][1:] == (0, 1)


def test_empty_album_has_no_cover(repo):
    service = ViewService(repo)
    repo.members = []
    assert service.get_cover_full_path(uuid4()) is None


def test_virtual_trash_uses_scope_cover(repo):
    service = ViewService(repo)
    cover = _asset()
    repo.scope_cover = cover
    repo.members = [_asset()]  # 不应被用到

    path = service.get_cover_full_path(VIRTUAL_ALBUM_TRASH_UUID)

    assert path == f"/photos/{cover.uuid}.jpg"
    assert repo.list_calls == []


def test_fallback_announces_viewed_for_on_demand_thumbnails(repo):
    """兜底取封面时也要广播 ``assets_viewed``（封面缩略图按需生成）。"""
    service = ViewService(repo)
    newest = _asset()
    repo.members = [newest]
    seen: list[tuple] = []
    service.assets_viewed.connect(
        lambda _sender, *, asset_uuids: seen.append(asset_uuids))

    service.get_cover_full_path(uuid4())

    assert seen and seen[0] == (newest.uuid,)
