#
"""Presenter 流程测试：用假服务驱动真实的 presenter 树。

覆盖主 presenter 的编排 + 各子 presenter 的关键路径（不需要数据库）：

* 相册列表 → 内容网格（uuid 列表、按需缩略图、懒信息）；
* 网格激活 → 详情页（元信息 / 导航条 / 大图）；
* 收藏 / 软删 / 恢复的写操作 → 内容网格刷新；
* 标签：当前资产 chip、打标（``父/子`` 路径）、chip 点击 → 过滤；
* 搜索：输入 → 候选 / 唯一命中 → 过滤意图；清空 → 清除过滤；
* 导入源：卡片列表、草稿新建、PATCH、启用禁用；
* 语言切换：主 presenter 收到 ``LanguageChange`` 后逐个重算。

``QT_QPA_PLATFORM=offscreen`` 必须在 import PySide6 之前设好；整份测试复用
进程里唯一的 ``QApplication``。
"""

from __future__ import annotations

import os
from contextlib import suppress
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.setStyleSheet("")


@pytest.fixture
def presenter_registry():
    """快照 / 恢复全局 presenter 工厂表。"""
    from albuswall.ui.presenter import base

    saved = list(base.PRESENTER_FACTORIES)
    try:
        yield saved
    finally:
        base.PRESENTER_FACTORIES[:] = saved


@pytest.fixture
def make_image(tmp_path):
    """写一张真实 PNG（缩略图 / 原图加载必须能解码）。"""
    from PySide6.QtGui import QPixmap

    counter = {"n": 0}

    def _make(name: str = "img.png", *, size: int = 8,
              width: int | None = None, height: int | None = None) -> str:
        counter["n"] += 1
        pixmap = QPixmap(width or size, height or size)
        pixmap.fill()
        path = tmp_path / f"{counter['n']}_{name}"
        pixmap.save(str(path), "PNG")
        return str(path)

    return _make


def pump(qapp, rounds: int = 40):
    """跑几轮事件循环（后台解码 → 队列投递回主线程）。"""
    import time

    for _ in range(rounds):
        qapp.processEvents()
        time.sleep(0.002)


# ==================== 假依赖 ====================

class FakeCustomSignal:
    """项目自定义 ``Signal`` 的最小替身：``cb(sender, **kwargs)``。"""

    def __init__(self) -> None:
        self._slots = []

    def connect(self, cb) -> None:
        if cb not in self._slots:
            self._slots.append(cb)

    def disconnect(self, cb) -> None:
        if cb in self._slots:
            self._slots.remove(cb)

    def emit(self, sender, **kwargs) -> None:
        for cb in list(self._slots):
            cb(sender, **kwargs)


def make_asset(uuid=None, *, title="a.jpg", favorite=False, deleted=False):
    from albuswall.dto.album import Asset

    return Asset(
        uuid=uuid or uuid4(),
        file_path=f"/photos/{title}",
        original_name=title,
        mime_type="image/jpeg",
        file_hash="deadbeef",
        source_id=0,
        is_favorite=favorite,
        is_deleted=deleted,
    )


def make_album(title="相册", description=""):
    from albuswall.dto.album import Album

    return Album(uuid=uuid4(), title=title, description=description)


class FakeViewService:
    """``ViewService`` 的最小替身（uuid 键，行为与真服务一致）。"""

    def __init__(self, albums=None, assets=None, paths=None, trash=None):
        self.albums = list(albums or ()) + list(trash or ())
        self.trash = list(trash or ())
        self.assets = list(assets or ())
        self.paths = dict(paths or {})
        self.by_uuid = {str(a.uuid): a for a in self.assets}
        self.deleted_scope: set[str] = {str(a.uuid) for a in self.trash}
        self.calls: list[tuple] = []

    # ---- 相册 ----
    def list_album_uuids(self):
        return [album.uuid for album in self.albums]

    def get_album(self, album_uuid):
        for album in self.albums:
            if str(album.uuid) == str(album_uuid):
                return album
        return None

    def get_cover_full_path(self, album_uuid):
        return self.paths.get(f"cover:{album_uuid}")

    def get_scope(self, album_uuid):
        from albuswall.common.constants.albums import VIRTUAL_ALBUM_ALL_UUID

        key = str(album_uuid)
        if key in self.deleted_scope:
            return "deleted"
        if key == str(VIRTUAL_ALBUM_ALL_UUID):
            return "active"
        return None

    # ---- 资产 ----
    def get_asset_uuids(self, album):
        return [asset.uuid for asset in self.assets]

    def get_asset(self, asset_uuid, *, include_deleted=False):
        self.calls.append(("get_asset", str(asset_uuid)))
        return self.by_uuid.get(str(asset_uuid))

    def get_asset_full_path(self, asset_uuid, *, include_deleted=False):
        return self.paths.get(str(asset_uuid))

    def get_asset_neighbours(self, album_uuid, asset_uuid):
        uuids = [str(a.uuid) for a in self.assets]
        try:
            index = uuids.index(str(asset_uuid))
        except ValueError:
            return None, None, 0, 0
        prev_uuid = uuids[index - 1] if index > 0 else None
        next_uuid = uuids[index + 1] if index + 1 < len(uuids) else None
        return prev_uuid, next_uuid, index + 1, len(uuids)


class FakeThumbnailService:
    def __init__(self) -> None:
        self.thumbnail_ready = FakeCustomSignal()
        self.paths: dict[str, str] = {}
        self.requested: list[tuple[str, bool]] = []

    def get_thumbnail_path(self, asset_uuid, spec=None):
        return self.paths.get(str(asset_uuid))

    def get_thumbnail_paths(self, uuids, spec=None):
        return {str(u): self.paths[str(u)] for u in uuids if str(u) in self.paths}

    def request(self, asset_uuid, *, include_deleted=False):
        self.requested.append((str(asset_uuid), include_deleted))
        return True

    def request_bulk(self, uuids, *, include_deleted=False, limit=None):
        for u in uuids:
            self.requested.append((str(u), include_deleted))
        return len(uuids)


class FakeAssetOpsService:
    def __init__(self, view_service) -> None:
        self._view = view_service
        self.favorite_calls: list[tuple[str, bool]] = []
        self.trash_calls: list[list[str]] = []
        self.restore_calls: list[list[str]] = []
        self.move_calls: list[tuple[str, str, list[str]]] = []
        self.remove_calls: list[tuple[str, list[str]]] = []
        self.add_calls: list[tuple[str, list[str]]] = []

    def set_favorite(self, asset_uuid, favorite):
        from albuswall.service.asset.command.ops import FavoriteResult

        self.favorite_calls.append((str(asset_uuid), bool(favorite)))
        asset = self._view.by_uuid.get(str(asset_uuid))
        if asset is not None:
            object.__setattr__(asset, "is_favorite", bool(favorite))
        return FavoriteResult(
            asset_uuid=UUID(str(asset_uuid)), outcome="updated",
            previous_value=not favorite)

    def trash(self, asset_uuids):
        from albuswall.service.asset.command.ops import TrashResult

        uuids = [str(u) for u in asset_uuids]
        self.trash_calls.append(uuids)
        for uuid in uuids:
            asset = self._view.by_uuid.get(uuid)
            if asset is not None:
                object.__setattr__(asset, "is_deleted", True)
        return TrashResult(changed=tuple(UUID(u) for u in uuids))

    def restore(self, asset_uuids):
        from albuswall.service.asset.command.ops import RestoreResult

        uuids = [str(u) for u in asset_uuids]
        self.restore_calls.append(uuids)
        for uuid in uuids:
            asset = self._view.by_uuid.get(uuid)
            if asset is not None:
                object.__setattr__(asset, "is_deleted", False)
        return RestoreResult(restored=tuple(UUID(u) for u in uuids))

    def move_between_albums(self, src_album_uuid, dst_album_uuid, asset_uuids):
        from albuswall.dto.asset.events import MoveResult

        uuids = [str(u) for u in asset_uuids]
        self.move_calls.append((str(src_album_uuid), str(dst_album_uuid), uuids))
        return MoveResult(moved=tuple(UUID(u) for u in uuids))

    def remove_from_album(self, album_uuid, asset_uuids):
        from albuswall.dto.asset.events import MembershipResult

        uuids = [str(u) for u in asset_uuids]
        self.remove_calls.append((str(album_uuid), uuids))
        return MembershipResult(removed=tuple(UUID(u) for u in uuids))

    def add_to_album(self, album_uuid, asset_uuids):
        from albuswall.dto.asset.events import MembershipResult

        uuids = [str(u) for u in asset_uuids]
        self.add_calls.append((str(album_uuid), uuids))
        return MembershipResult(added=tuple(UUID(u) for u in uuids))


class FakeTag:
    def __init__(self, name, *, parent=None):
        self.uuid = uuid4()
        self.name = name
        self.display_name = name
        self.description = ""
        self.parent_uuid = parent


class FakeTagService:
    def __init__(self) -> None:
        self.tree: list = []
        self.ensure_path_calls: list[str] = []
        self.created: list[str] = []

    def list_tree(self):
        return list(self.tree)

    def ensure_path(self, path):
        self.ensure_path_calls.append(path)
        tag = FakeTag(path.split("/")[-1])
        self.created.append(path)
        return tag.uuid

    def path_label(self, tag_uuid):
        return f"label:{tag_uuid}"

    def search(self, keyword, *, album_uuid=None, scope="active", limit=20):
        from albuswall.dto.tag.query import TagMatch

        tag = FakeTag(keyword)
        return [TagMatch(tag=tag, asset_count=2, path=keyword)]

    def get(self, tag_uuid):
        return FakeTag("tag")

    def create(self, name, parent_uuid=None):
        self.created.append(name)
        return uuid4()

    def rename(self, tag_uuid, new_name):
        self.created.append(f"rename:{new_name}")

    def delete(self, tag_uuid, *, cascade=False):
        self.created.append(f"delete:{tag_uuid}")

    def restore(self, tag_uuid):
        from albuswall.service.tag import RestoreResult

        return RestoreResult(tag=FakeTag("tag"), parent_visible=True)


class FakeAssetTagService:
    def __init__(self) -> None:
        self.bindings: dict[str, list] = {}
        self.removed: list[tuple[str, str]] = []

    def list_of_asset(self, asset_uuid):
        return list(self.bindings.get(str(asset_uuid), ()))

    def add(self, asset_uuid, tag_uuids):
        from albuswall.service.asset.command.tagging import AddTagsResult

        added = 0
        for tag_uuid in tag_uuids:
            target = self.bindings.setdefault(str(asset_uuid), [])
            if all(str(t.uuid) != str(tag_uuid) for t in target):
                target.append(FakeTag("bound", parent=None))
                added += 1
        return AddTagsResult(added=added, existing=0, skipped_uuids=())

    def remove(self, asset_uuid, tag_uuids):
        target = self.bindings.get(str(asset_uuid), [])
        before = len(target)
        self.bindings[str(asset_uuid)] = [
            tag for tag in target
            if all(str(tag.uuid) != str(tag_uuid) for tag_uuid in tag_uuids)
        ]
        self.removed.extend(
            (str(asset_uuid), str(tag_uuid)) for tag_uuid in tag_uuids)
        return before - len(self.bindings[str(asset_uuid)])


class FakeAlbumService:
    """``AlbumService`` 的最小替身：记录 create / rename 的入参。"""

    def __init__(self, view_service=None) -> None:
        self._view = view_service
        self.created: list = []
        self.renamed: list[tuple[str, str]] = []

    def create(self, payload):
        self.created.append(payload)
        album = make_album(payload.title, getattr(payload, "description", ""))
        if self._view is not None:
            self._view.albums.append(album)
        return album

    def rename(self, album_uuid, new_title):
        self.renamed.append((str(album_uuid), new_title))
        if self._view is not None:
            album = self._view.get_album(album_uuid)
            if album is not None:
                object.__setattr__(album, "title", new_title)
        return True


class FakeSourceService:
    def __init__(self) -> None:
        from albuswall.dto.source.query import IngestSource

        self.sources = [IngestSource(id=1, title="源一", source_path="/photos")]
        self.creates: list = []
        self.updates: list = []
        self.enabled: list[int] = []
        self.disabled: list[int] = []
        self.deleted: list[int] = []

    def list_sources(self):
        return list(self.sources)

    def get_source(self, source_id):
        for source in self.sources:
            if source.id == source_id:
                return source
        return None

    def create(self, payload):
        self.creates.append(payload)
        return 99

    def update(self, source_id, patch):
        self.updates.append((source_id, patch))

    def enable(self, source_id):
        self.enabled.append(source_id)

    def disable(self, source_id):
        self.disabled.append(source_id)

    def delete(self, source_id):
        self.deleted.append(source_id)


# ==================== 装配辅助 ====================

class Stack:
    """一整套 fake 服务 + 装配好的 presenter 树。"""

    def __init__(self, window, presenter, view, thumbs, ops, tags, asset_tags,
                 sources, albums):
        self.window = window
        self.presenter = presenter
        self.view = view
        self.thumbs = thumbs
        self.ops = ops
        self.tags = tags
        self.asset_tags = asset_tags
        self.sources = sources
        self.albums = albums

    def child(self, name):
        child = self.presenter.child(name)
        assert child is not None
        return child


def build_stack(qapp, *, albums=None, assets=None, paths=None, trash=None):
    from albuswall.core import Container
    from albuswall.ui.presenter import MainPresenter
    from albuswall.ui.window import Window

    view = FakeViewService(albums, assets, paths, trash)
    thumbs = FakeThumbnailService()
    ops = FakeAssetOpsService(view)
    tags = FakeTagService()
    asset_tags = FakeAssetTagService()
    sources = FakeSourceService()
    album_service = FakeAlbumService(view)

    container = Container()
    container.set("album_service", album_service)
    container.set("view_service", view)
    container.set("thumbnail_service", thumbs)
    container.set("asset_ops_service", ops)
    container.set("tag_service", tags)
    container.set("asset_tag_service", asset_tags)
    container.set("source_service", sources)

    window = Window()
    presenter = MainPresenter.bootstrap(container, app=qapp, window=window)
    return Stack(window, presenter, view, thumbs, ops, tags, asset_tags,
                 sources, album_service)


@pytest.fixture
def stack(qapp, presenter_registry):
    stacks: list[Stack] = []

    def _make(*args, **kwargs):
        built = build_stack(qapp, *args, **kwargs)
        stacks.append(built)
        return built

    yield _make

    for built in stacks:
        try:
            built.presenter.stop()
        finally:
            built.window.close()
    qapp.setStyleSheet("")


# ==================== 相册 → 内容网格 ====================

def test_album_list_populates_and_selects_first_album(stack, qapp):
    album = make_album("旅行", "我的照片")
    built = stack(albums=[album], assets=[])

    album_p = built.child("album")
    content = built.child("content")

    # 相册列表
    assert album_p.current is not None
    assert album_p.current.title == "旅行"
    assert album_p.model.rowCount() == 1
    # 标题栏 / 窗口标题
    assert built.window.title_bar.title_label.text() == "旅行"
    assert built.window.title_bar.sub_title.text() == "我的照片"
    assert built.window.windowTitle() == "旅行"
    # 内容网格拿到的是同一个相册
    assert content.album_uuid == album.uuid


def test_content_grid_loads_assets_and_requests_thumbnails(
        stack, qapp, make_image):
    first = make_asset(title="one.jpg", favorite=True)
    second = make_asset(title="two.jpg")
    built = stack(albums=[make_album()], assets=[first, second])

    content = built.child("content")
    assert content.model.rowCount() == 2

    # 信息懒加载（含收藏态）
    pump(qapp)
    assert content.model.uuid_at(0) == str(first.uuid)
    assert content.model.entry_at(0).title == "one.jpg"
    assert content.model.entry_at(0).favorite is True

    # 没有缩略图路径 → 向缩略图服务请求生成
    assert any(uuid == str(first.uuid) for uuid, _ in built.thumbs.requested)

    # 路径可用 → 后台解码后回填 pixmap
    built.thumbs.paths[str(first.uuid)] = make_image("thumb.png")
    built.thumbs.thumbnail_ready.emit(None, asset_uuid=str(first.uuid))
    pump(qapp)
    assert content.model.pixmap_at(0) is not None


def test_album_switch_refreshes_content_grid(stack, qapp):
    first = make_album("A")
    second = make_album("B")
    built = stack(albums=[first, second], assets=[make_asset()])

    content = built.child("content")
    album_p = built.child("album")
    album_p.set_album(second)

    assert content.album_uuid == second.uuid
    assert built.window.title_bar.title_label.text() == "B"


# ==================== 网格 → 详情页 ====================

def test_asset_activation_opens_detail(stack, qapp, make_image):
    asset = make_asset(title="one.jpg")
    built = stack(
        albums=[make_album()],
        assets=[asset],
        paths={str(asset.uuid): make_image("full.png")},
    )

    content = built.child("content")
    viewer = built.child("viewer")

    content.asset_activated.emit(str(asset.uuid))
    pump(qapp)

    assert viewer.current_asset_uuid == str(asset.uuid)
    assert built.window.overlay.currentWidget() is built.window.detail
    # 导航条：一张
    assert viewer._nav_model.rowCount() == 1
    # 大图已解码
    assert not built.window.detail.image_viewer._pixmap.isNull()


def test_detail_favorite_trash_recover_write_through(stack, qapp):
    asset = make_asset(title="one.jpg")
    built = stack(albums=[make_album()], assets=[asset])

    viewer = built.child("viewer")
    content = built.child("content")
    built.child("content").asset_activated.emit(str(asset.uuid))
    pump(qapp)

    # 收藏
    built.window.detail.favorite_toggle_requested.emit(True)
    assert built.ops.favorite_calls == [(str(asset.uuid), True)]
    assert viewer._is_favorite is True

    # 软删 → 没有下一张 → 关闭详情
    built.window.detail.trash_requested.emit()
    assert built.ops.trash_calls == [[str(asset.uuid)]]
    assert built.window.overlay.is_blank()

    # 恢复（重新打开详情；软删态下 include_deleted=True）
    from albuswall.ui.presenter import ViewerContext

    viewer.open(ViewerContext(
        album_uuid=built.child("album").current_uuid,
        current_asset_uuid=str(asset.uuid),
        include_deleted=True,
    ))
    built.window.detail.recover_requested.emit()
    assert built.ops.restore_calls == [[str(asset.uuid)]]
    assert content.model.rowCount() == 1


# ==================== 标签 ====================

def test_tagbar_binds_and_filters(stack, qapp):
    asset = make_asset(title="one.jpg")
    built = stack(albums=[make_album()], assets=[asset])

    tagbar = built.child("tagbar")
    search = built.child("search")
    content = built.child("content")

    # 光标资产 → 标签作用对象
    built.child("content").selection_changed.emit(str(asset.uuid))
    assert tagbar.current_asset_uuid == str(asset.uuid)

    # 输入「父/子」路径 → ensure_path → 绑定 → chip 出现
    built.window.detail.tag_bar.apply_requested.emit("旅行/日本")
    assert built.tags.ensure_path_calls == ["旅行/日本"]
    assert tagbar._chip_model.rowCount() == 1

    # chip 点击 → 搜索过滤
    filters: list = []
    search.filter_requested.connect(filters.append)
    built.window.detail.tag_bar.chip_activated.emit("tag-uuid")
    assert filters == ["tag-uuid"]
    # 过滤意图 → 内容网格按标签重算
    search.filter_requested.emit("tag-uuid")
    assert content.tag_filter == "tag-uuid"


def test_tagbar_remove_and_tree_activation(stack, qapp):
    asset = make_asset(title="one.jpg")
    built = stack(albums=[make_album()], assets=[asset])
    tagbar = built.child("tagbar")

    built.child("content").selection_changed.emit(str(asset.uuid))
    built.window.detail.tag_bar.apply_requested.emit("x")
    assert tagbar._chip_model.rowCount() == 1

    bound_uuid = str(tagbar._chip_model.uuid_at(0))
    built.window.detail.tag_bar.chip_remove_requested.emit(bound_uuid)
    assert tagbar._chip_model.rowCount() == 0
    assert built.asset_tags.removed == [(str(asset.uuid), bound_uuid)]


# ==================== 搜索 ====================

def test_search_suggestions_and_clear(stack, qapp):
    built = stack(albums=[make_album()], assets=[])
    search = built.child("search")
    view = built.window.title_bar.floating_search_bar

    fired: list = []
    search.filter_requested.connect(fired.append)

    view.text_changed.emit("旅行")
    pump(qapp)
    assert view.option_combo.count() >= 2  # 建议列表已弹出（视图内部）

    view.search.emit({"text": "旅行", "option": view.MODE_SIMPLE})
    assert fired and fired[0] is not None

    view.text_changed.emit("")
    assert fired[-1] is None


# ==================== 导入源 ====================

def test_source_page_cards_and_apply(stack, qapp):
    built = stack(albums=[], assets=[])
    source = built.child("source")
    page = built.window.source

    assert source.model.rowCount() == 1
    assert source.model.entry_at(0).title == "源一"

    # 点第一张卡片 → 表单回填
    page.card_clicked.emit(source.model.index(0, 0))
    assert page.detail_widget.title_line_edit.text() == "源一"

    # 改标题 → 应用（PATCH）
    page.detail_widget.title_line_edit.setText("改名")
    page.apply_button.click()
    assert built.sources.updates
    source_id, patch = built.sources.updates[-1]
    assert source_id == 1
    assert patch.title == "改名"


def test_source_draft_create(stack, qapp):
    built = stack(albums=[], assets=[])
    source = built.child("source")
    page = built.window.source

    page.add_button.click()
    assert source.model.rowCount() == 2  # 草稿行 + 已存在的源

    page.detail_widget.title_line_edit.setText("新源")
    page.detail_widget.source_path_line_edit.setText("/data/pics")
    page.apply_button.click()

    assert built.sources.creates
    payload = built.sources.creates[-1]
    assert payload.title == "新源"
    assert payload.source_path == "/data/pics"


def test_source_disable_and_delete(stack, qapp):
    built = stack(albums=[], assets=[])
    source = built.child("source")

    # 先用 PATCH 路径确认服务可用
    source.set_source_disabled(1, True)
    assert built.sources.disabled == [1]
    source.set_source_disabled(1, False)
    assert built.sources.enabled == [1]

    assert source.delete_source(1) is True
    assert built.sources.deleted == [1]


# ==================== 语言切换 ====================

def test_language_change_retranslates_every_presenter(stack, qapp):
    album = make_album("旅行")
    built = stack(albums=[album], assets=[])
    album_p = built.child("album")

    calls: list[str] = []
    original = album_p.refresh

    def _spy():
        calls.append("album")
        original()

    album_p.refresh = _spy  # type: ignore[method-assign]
    with suppress(Exception):
        built.presenter.retranslate()
    assert "album" in calls


# ==================== 窗口级反馈 / 相册增改 / 标签编辑 ====================

def test_error_signal_routes_to_window_toast(stack, qapp):
    built = stack(albums=[make_album()], assets=[])
    built.child("viewer").error.emit("出错了")
    assert built.window.toast_label is not None
    assert built.window.toast_label.text() == "出错了"


def test_viewer_navigate_relative(stack, qapp, make_image):
    first = make_asset(title="1.jpg")
    second = make_asset(title="2.jpg")
    built = stack(
        albums=[make_album()],
        assets=[first, second],
        paths={
            str(first.uuid): make_image("1.png"),
            str(second.uuid): make_image("2.png"),
        },
    )
    viewer = built.child("viewer")
    built.child("content").asset_activated.emit(str(first.uuid))
    pump(qapp)

    assert viewer.navigate_relative(1) is True
    assert viewer.current_asset_uuid == str(second.uuid)
    assert viewer.navigate_relative(1) is False  # 已经是最后一张
    assert viewer.navigate_relative(-1) is True
    assert viewer.current_asset_uuid == str(first.uuid)


def test_album_create_and_rename(stack, qapp, monkeypatch):
    built = stack(albums=[make_album("旧名")], assets=[])
    album_p = built.child("album")

    monkeypatch.setattr(album_p, "_ask_title", lambda *_a, **_k: "新相册")
    built.window.album.add_button.click()
    assert built.albums.created
    assert built.albums.created[-1].title == "新相册"
    assert album_p.current_uuid is not None

    monkeypatch.setattr(album_p, "_ask_title", lambda *_a, **_k: "改名后")
    built.window.album.edit_button.click()
    assert built.albums.renamed
    assert built.albums.renamed[-1][1] == "改名后"


def test_tag_editor_create_rename_delete(stack, qapp, monkeypatch):
    from albuswall.ui.presenter import tag_editor_presenter as mod

    class FakeDialog:
        DialogCode = type("Code", (), {"Accepted": 1})

        def __init__(self, mode, *, initial="", title=None, parent=None):
            self._value = "新标签"

        def exec(self):
            return 1

        def value(self):
            return self._value

    monkeypatch.setattr(mod, "TagEditDialog", FakeDialog)
    built = stack(albums=[make_album()], assets=[])
    editor = built.child("tag_editor")

    editor.create("")
    editor.rename("tag-uuid")
    editor.delete("tag-uuid")

    assert "新标签" in built.tags.created
    assert any(item.startswith("rename:") for item in built.tags.created)
    assert any(item.startswith("delete:") for item in built.tags.created)


# ==================== 版式 / 正方形缩略图 ====================

def test_album_page_top_holds_albums_and_bottom_holds_trash(stack, qapp, tmp_path):
    from albuswall.ui.presenter import AlbumPresenter  # noqa: F401

    album = make_album("旅行")
    trash = make_album("回收站")
    built = stack(albums=[album], assets=[], trash=[trash])
    album_p = built.child("album")
    page = built.window.album

    # 顶部：相册网格（不含回收站）
    assert album_p.model.rowCount() == 1
    assert album_p.model.uuid_at(0) == str(album.uuid)
    assert page.album_view.model() is album_p.model
    # 底部：回收站
    assert album_p.trash_model.rowCount() == 1
    assert album_p.trash_model.uuid_at(0) == str(trash.uuid)
    assert page.trash_view.model() is album_p.trash_model

    # 切到回收站：标题栏跟随，高亮落在底部列表
    album_p.set_album_uuid(trash.uuid)
    assert album_p.current_uuid == trash.uuid
    assert page.trash_view.currentIndex().row() == 0


def test_square_pixmap_helper():
    from PySide6.QtGui import QPixmap

    from albuswall.ui.presenter.thumbnails import square_pixmap

    wide = QPixmap(20, 10)
    square = square_pixmap(wide)
    assert (square.width(), square.height()) == (10, 10)

    scaled = square_pixmap(wide, edge=64)
    assert (scaled.width(), scaled.height()) == (64, 64)

    # 已经方且尺寸相同：原样返回
    already = QPixmap(32, 32)
    assert square_pixmap(already, edge=32) is already
    assert square_pixmap(None).isNull()


def test_content_thumbnail_is_square(stack, qapp, make_image):
    asset = make_asset(title="wide.jpg")
    built = stack(albums=[make_album()], assets=[asset])

    content = built.child("content")
    built.thumbs.paths[str(asset.uuid)] = make_image(
        "wide.png", width=32, height=8)
    built.thumbs.thumbnail_ready.emit(None, asset_uuid=str(asset.uuid))
    pump(qapp)

    pixmap = content.model.pixmap_at(0)
    assert pixmap is not None
    assert pixmap.width() == pixmap.height()


def test_nav_thumbnail_is_square(stack, qapp, make_image):
    asset = make_asset(title="wide.jpg")
    built = stack(
        albums=[make_album()],
        assets=[asset],
        paths={str(asset.uuid): make_image("full.png")},
    )
    built.thumbs.paths[str(asset.uuid)] = make_image(
        "wide-thumb.png", width=32, height=8)

    viewer = built.child("viewer")
    built.child("content").asset_activated.emit(str(asset.uuid))
    pump(qapp)

    row = viewer._nav_model.row_of(str(asset.uuid))
    pixmap = viewer._nav_model.pixmap_at(row)
    assert pixmap is not None
    assert pixmap.width() == pixmap.height()


def test_album_cover_is_square(stack, qapp, make_image):
    album = make_album("旅行")
    built = stack(
        albums=[album],
        assets=[],
        paths={f"cover:{album.uuid}": make_image(
            "cover.png", width=32, height=8)},
    )
    pump(qapp)

    album_p = built.child("album")
    pixmap = album_p.model.pixmap_at(0)
    assert pixmap is not None
    assert pixmap.width() == pixmap.height()


def test_content_is_the_hit_target_and_detail_reopens(stack, qapp):
    """真实鼠标命中路径：内容层必须能收到点击 / 滚轮（回归：
    模糊取景层曾铺满整窗且不透明鼠标事件，把内容层的事件全吞了）。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    asset = make_asset(title="one.jpg")
    built = stack(albums=[make_album()], assets=[asset])
    window = built.window
    window.resize(960, 720)
    window.show()
    pump(qapp)

    # 标题栏之下、第一行第一列（只有一张资产时也只命中它）
    point = QPoint(20, window.title_bar.height() + 40)
    target = QApplication.widgetAt(window.mapToGlobal(point))
    assert target is window.content, (
        f"expected content under the cursor, got {type(target).__name__}")

    for _ in range(2):
        QTest.mouseClick(
            target, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, target.mapFromGlobal(
                window.mapToGlobal(point)))
        pump(qapp)
        assert not window.overlay.is_blank()
        window.detail.close_requested.emit()
        pump(qapp)
        assert window.overlay.is_blank()


# ==================== 主题图标 / 样式表选择器 ====================

def test_theme_icons_are_applied_to_window(stack, qapp):
    built = stack(albums=[], assets=[])
    title_bar = built.window.title_bar
    detail = built.window.detail

    for name, button in (
            ("album", title_bar.album_button),
            ("search", title_bar.search_button),
            ("extra", title_bar.extra_button),
    ):
        assert title_bar.button_icon_mode(name) is True
        assert not button.icon().isNull(), f"{name} icon missing"
        assert button.text() == ""
        assert button.toolTip()  # 文案落到 tooltip

    favourite = detail.favourite_button
    assert detail.is_action_icon_mode(favourite) is True
    assert not favourite.icon().isNull()
    assert favourite.toolTip() == "收藏"

    # 窗口 / 应用图标
    assert not built.window.windowIcon().isNull()
    assert not qapp.windowIcon().isNull()


def test_icon_buttons_fall_back_to_text_without_theme_icons(stack, qapp):
    from albuswall.ui.presenter.theme import Theme

    built = stack(albums=[], assets=[])
    window_p = built.child("window")
    window_p.apply_theme(Theme(name="empty", source="empty"))

    title_bar = built.window.title_bar
    assert title_bar.button_icon_mode("album") is False
    assert title_bar.album_button.icon().isNull()
    assert title_bar.album_button.text() == "相册"

    favourite = built.window.detail.favourite_button
    assert built.window.detail.is_action_icon_mode(favourite) is False
    assert favourite.icon().isNull()
    assert favourite.text() == "收藏"


def test_theme_qss_selectors_match_widget_object_names(stack, qapp):
    """样式表里引用的 ``#objectName`` 必须真的存在于控件树（回归）。"""
    import re

    from albuswall import resources
    from PySide6.QtWidgets import QWidget

    asset = make_asset(title="one.jpg")
    built = stack(albums=[make_album()], assets=[asset])
    window = built.window
    names = {window.objectName()}
    names.update(child.objectName() for child in window.findChildren(QWidget))
    # 右键菜单是运行时才构造的弹出层，不在常驻控件树里
    menu = built.child("content").build_asset_menu(str(asset.uuid))
    names.add(menu.objectName())
    for action in menu.actions():
        submenu = action.menu()
        if submenu is not None:
            names.add(submenu.objectName())

    qss = resources.qss.read_text(encoding="utf-8")
    stripped = re.sub(r"/\*.*?\*/", "", qss, flags=re.S)
    ids = set(re.findall(r"#([A-Za-z_][A-Za-z0-9_]*)", stripped))

    hex_color = re.compile(r"[0-9A-Fa-f]{6,8}")
    # 设置页尚未重建（#Setting* 规则为将来保留），其余必须是真实控件名
    stale = {
        name for name in ids
        if name not in names
        and not hex_color.fullmatch(name)
        and not name.startswith("Setting")
    }
    assert not stale, f"QSS 引用了不存在的 objectName: {sorted(stale)}"

    # 相册页的两个视图必须各自有样式入口（注释里的旧名映射不算规则）
    assert "#AlbumAlbumView" in stripped
    assert "#AlbumTrashView" in stripped
    assert "#AlbumView {" not in stripped
    assert "#AlbumMoreView" not in stripped


# ==================== 内容页右键菜单 ====================

def test_content_context_menu_has_move_and_remove(stack, qapp):
    album = make_album("旅行")
    other = make_album("Ricing")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album, other], assets=[asset])
    content = built.child("content")

    menu = content.build_asset_menu(str(asset.uuid))
    texts = [action.text() for action in menu.actions()]

    assert "移动到指定相册" in texts
    assert "从本相册移除" in texts
    move_menu = menu.actions()[0].menu()
    move_texts = [action.text() for action in move_menu.actions()]
    assert "Ricing" in move_texts          # 其它相册是可选目标
    assert "旅行" not in move_texts        # 当前相册不出现
    assert "新建相册并移入…" in move_texts


def test_context_menu_move_copies_to_target_album(stack, qapp):
    album = make_album("旅行")
    other = make_album("Ricing")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album, other], assets=[asset])
    content = built.child("content")

    menu = content.build_asset_menu(str(asset.uuid))
    move_menu = menu.actions()[0].menu()
    action = next(a for a in move_menu.actions() if a.text() == "Ricing")
    action.trigger()

    assert built.ops.move_calls == [
        (str(album.uuid), str(other.uuid), [str(asset.uuid)])]
    # 写成功 → 当前列表重拉 + 相册封面刷新信号
    assert content.model.rowCount() == 1


def test_context_menu_remove_from_album(stack, qapp):
    album = make_album("旅行")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album], assets=[asset])
    content = built.child("content")

    menu = content.build_asset_menu(str(asset.uuid))
    remove_action = next(a for a in menu.actions() if a.text() == "从本相册移除")
    remove_action.trigger()

    assert built.ops.remove_calls == [(str(album.uuid), [str(asset.uuid)])]


def test_context_menu_create_album_and_move(stack, qapp, monkeypatch):
    album = make_album("旅行")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album], assets=[asset])
    content = built.child("content")
    monkeypatch.setattr(content, "_ask_new_album_title", lambda: "新相册")

    menu = content.build_asset_menu(str(asset.uuid))
    move_menu = menu.actions()[0].menu()
    create = next(
        a for a in move_menu.actions() if a.text() == "新建相册并移入…")
    create.trigger()

    assert built.albums.created
    new_uuid = str(built.view.albums[-1].uuid)
    assert built.ops.move_calls == [
        (str(album.uuid), new_uuid, [str(asset.uuid)])]


def test_context_menu_disabled_on_trash_album(stack, qapp):
    trash = make_album("回收站")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[], assets=[asset], trash=[trash])
    album_p = built.child("album")
    album_p.set_album_uuid(trash.uuid)
    content = built.child("content")

    menu = content.build_asset_menu(str(asset.uuid))
    actions = {action.text(): action for action in menu.actions() if action.text()}
    assert actions["添加到指定相册"].isEnabled() is False
    assert actions["从本相册移除"].isEnabled() is False


def test_context_menu_on_all_view_adds_instead_of_moving(stack, qapp):
    """「全部」是聚合视图：右键只能「添加到相册」，不能移动 / 移除。"""
    from albuswall.common.constants.albums import VIRTUAL_ALBUM_ALL_UUID
    from albuswall.dto.album import Album

    album = make_album("旅行")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album], assets=[asset])
    content = built.child("content")
    # 切到虚拟「全部」视图（scope == active）
    content.set_album(Album(uuid=VIRTUAL_ALBUM_ALL_UUID, title="All"))
    qapp.processEvents()

    menu = content.build_asset_menu(str(asset.uuid))
    texts = [action.text() for action in menu.actions()]
    assert "添加到指定相册" in texts
    assert "从本相册移除" in texts
    remove_action = next(a for a in menu.actions() if a.text() == "从本相册移除")
    assert remove_action.isEnabled() is False

    move_menu = move_menu_of(menu)
    action = next(a for a in move_menu.actions() if a.text() == "旅行")
    action.trigger()
    assert built.ops.add_calls == [(str(album.uuid), [str(asset.uuid)])]
    assert built.ops.move_calls == []


def move_menu_of(menu):
    from PySide6.QtWidgets import QMenu

    for action in menu.actions():
        submenu = action.menu()
        if isinstance(submenu, QMenu):
            return submenu
    raise AssertionError("move submenu not found")


def test_notice_signals_reach_window_toast(stack, qapp):
    album = make_album("旅行")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album], assets=[asset])
    built.child("content").notice.emit("已移动")
    assert built.window.toast_label is not None
    assert built.window.toast_label.text() == "已移动"


def test_album_cover_refreshes_after_move(stack, qapp, make_image):
    """移入资产后，目标相册的封面要重新解析并回填（回归：坑在服务层兜底）。"""
    album = make_album("旅行")
    target = make_album("Ricing")
    asset = make_asset(title="one.jpg")
    built = stack(albums=[album, target], assets=[asset])

    content = built.child("content")
    album_p = built.child("album")
    thumb = make_image("cover.png", width=32, height=8)

    cover = {"path": None}
    built.view.get_cover_full_path = (
        lambda uuid, **kwargs: cover["path"]
        if str(uuid) == str(target.uuid) else None
    )

    row = album_p.model.row_of(str(target.uuid))
    assert row >= 0 and album_p.model.pixmap_at(row) is None

    cover["path"] = thumb                     # 移动后目标相册有了封面
    assert content.move_asset_to_album(str(asset.uuid), target.uuid) is True
    pump(qapp)

    pixmap = album_p.model.pixmap_at(row)
    assert pixmap is not None
    assert pixmap.width() == pixmap.height()   # 封面同样是正方形
