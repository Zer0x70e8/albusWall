#
"""pytest 共享 fixture。

三条硬约束：

* ``albuswall.command.CommandRegistry`` 的默认实例是**进程全局单例**
  （``get_default_registry()``），一旦 ``freeze()`` 就无法复用 —— 任何
  用例都不得 import 它来装配命令，一律用 ``fresh_registry`` 新建。
* handler 的依赖边界是 **service**（不是 repo）：stub 层放在 service 上，
  这样用例同时验证「handler 只跟 service 说话」这条契约。repo 是 service
  的内部实现细节，测试不感知。
* 绝不连真 DB、绝不碰 ``~/.local/share``。

只读观测命令（album/asset/tag/thumb/trash）仍是退役状态，对应的 service
stub 一并移除；当前在册的业务域是 ``task``（``FakeTaskService``）与
``source``（``FakeSourceService`` / ``FakeSourceStateService`` /
``FakeSourceRuntime`` / ``FakeSourceTrashService``）与 ``import``
（``FakeImportService``）。
"""

from __future__ import annotations

import types
from typing import Any
from uuid import UUID, uuid4

import pytest

from albuswall.command import CommandRegistry
from albuswall.core import Container
from albuswall.dto.thumbnail.paths import ThumbnailPaths
from albuswall.dto.thumbnail.query import MissingThumbnailRow
from albuswall.service.command.handlers import setup as setup_handlers


# ==================== 构造辅助 ====================

def make_missing_row(
        *,
        asset_id: int = 5,
        uuid: UUID | None = None,
        source_id: int | None = None,
        base: str | None = "/thumbs",
        small: str | None = "s.jpg",
) -> MissingThumbnailRow:
    """构造一个 ``MissingThumbnailRow``（含嵌套 ``ThumbnailPaths``）。"""
    return MissingThumbnailRow(
        id=asset_id,
        uuid=uuid or uuid4(),
        source_id=source_id,
        file_path="2024/a.jpg",
        file_hash="deadbeef",
        paths=ThumbnailPaths(base=base, small=small),
    )


# ==================== stub：service 层 ====================

class FakeTaskService:
    """``task_service`` 的最小 stub（infra 层服务，本身就是 service）。

    ``TaskServiceStats`` 在 ``__init__`` 里**局部 import**，不放在模块顶部：
    ``albuswall.dto.task`` 顶部会触发 ``albuswall.infrastructure.__init__``，
    后者又反向 import ``infrastructure.task.service`` → ``dto.task``，这是
    一个**既有**的循环 import（``dto/`` 与 ``infrastructure/`` 都不在本批
    白名单）。「谁先被 import」决定成败，所以延到调用期，保证 handlers
    （间接拉起 infrastructure）已经先加载完。
    """

    def __init__(
            self,
            *,
            submitted: int = 42,
            rejected: int = 2,
            backpressure_reject: int = 3,
            backpressure_wait: int = 1,
            last_backpressure_ts: float = 1700000000.5,
            queue_size: int = 7,
            global_concurrency_max: int = 8,
            global_concurrency_available: int = 5,
            global_concurrency_current: int = 3,
    ) -> None:
        from albuswall.dto.task import TaskServiceStats

        self._stats = TaskServiceStats(
            submitted=submitted,
            rejected=rejected,
            backpressure_reject=backpressure_reject,
            backpressure_wait=backpressure_wait,
            last_backpressure_ts=last_backpressure_ts,
        )
        self._queue_size = queue_size
        self._max = global_concurrency_max
        self._available = global_concurrency_available
        self._current = global_concurrency_current
        self.calls: list[str] = []

    def get_stats(self):
        self.calls.append("get_stats")
        return self._stats

    @property
    def queue_size(self) -> int:
        return self._queue_size

    @property
    def global_concurrency_max(self) -> int:
        return self._max

    @property
    def global_concurrency_available(self) -> int:
        return self._available

    @property
    def global_concurrency_current(self) -> int:
        return self._current


class FakeSourceService:
    """``source_service`` 的最小 stub：只记录写调用、读走内存表。

    写方法**不做任何线程处理**（真服务会回主线程）—— handler 层的用例只关心
    "调了谁、传了什么"，线程边界由 ``tests/test_source_thread_boundary.py``
    覆盖。
    """

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.created: list[Any] = []
        self.updates: list[tuple[int, Any]] = []
        self.deleted: list[int] = []
        self.enabled: list[int] = []
        self.disabled: list[int] = []

    # ---- 读 ----
    def list_sources(self) -> list[Any]:
        return list(self.rows)

    def get_source(self, source_id: int) -> Any:
        return next((r for r in self.rows if r.id == source_id), None)

    # ---- 写 ----
    def create(self, source: Any) -> int:
        self.created.append(source)
        return len(self.created)

    def update(self, source_id: int, patch: Any) -> None:
        self.updates.append((source_id, patch))

    def delete(self, source_id: int) -> None:
        self.deleted.append(source_id)

    def enable(self, source_id: int) -> None:
        self.enabled.append(source_id)

    def disable(self, source_id: int) -> None:
        self.disabled.append(source_id)


class FakeSourceRuntime:
    """``source_runtime`` 的最小 stub：记录运行控制调用、返回固定快照。"""

    def __init__(self) -> None:
        self.scanned: list[int] = []
        self.stopped: list[int] = []
        self.forced_seq = 7
        self.snapshots: list[dict] = []

    def force_scan(self, source):
        self.scanned.append(source.id)
        return self.forced_seq

    def stop_scan(self, source_id: int) -> bool:
        self.stopped.append(source_id)
        return True

    def status(self, source_id=None):
        rows = self.snapshots
        if source_id is not None:
            rows = [r for r in rows if r.source_id == source_id]
        return list(rows)


class FakeSourceStateService:
    """``source_state_service`` 的最小 stub：记录两个运行时入口。"""

    def __init__(self) -> None:
        self.refreshed: list[int] = []
        self.mounted: list[int] = []

    def refresh_source(self, source_id: int) -> None:
        self.refreshed.append(source_id)

    def mount_source(self, source_id: int) -> None:
        self.mounted.append(source_id)


class _NullSignal:
    """最小信号替身：只支持装配层用到的 ``connect`` / ``emit``。"""

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> Any:
        self.slots.append(slot)
        return slot

    def emit(self, sender: Any, **kwargs: Any) -> None:
        for slot in list(self.slots):
            slot(sender, **kwargs)


class _StubSignal:
    """极简信号替身：``connect`` / ``emit``（装配层只用到这两个）。"""

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> Any:
        self.slots.append(slot)
        return slot

    def emit(self, sender: Any, **kwargs: Any) -> None:
        for slot in list(self.slots):
            slot(sender, **kwargs)


class FakeViewService:
    """``view_service`` 最小 stub：一页资产 + ``assets_viewed`` 信号。"""

    def __init__(self) -> None:
        self.assets_viewed = _StubSignal()
        self.assets: list[Any] = []
        self.albums: dict[Any, Any] = {}
        self.listed: list[tuple[Any, int, int]] = []

    def list_assets(self, album_uuid, *, offset=0, limit=100):
        self.listed.append((album_uuid, offset, limit))
        return list(self.assets)

    def get_asset(self, asset_uuid, *, include_deleted=False):
        return next(
            (a for a in self.assets if str(a.uuid) == str(asset_uuid)), None,
        )

    def list_album_uuids(self):
        return list(self.albums)

    def get_album(self, album_uuid):
        return self.albums.get(str(album_uuid))

    def get_scope(self, album_uuid):
        return None

    def get_asset_uuids(self, album):
        return [a.uuid for a in self.assets]

    def get_asset_full_path(self, asset_uuid, *, include_deleted=False):
        return None

    def get_cover_full_path(self, album_uuid, *, include_deleted=False):
        return None


class FakeAlbumService:
    """``album_service`` 最小 stub：记录写调用。"""

    def __init__(self) -> None:
        self.created: list[Any] = []
        self.updates: list[tuple[Any, Any]] = []
        self.deleted: list[tuple[Any, bool]] = []
        self.restored: list[Any] = []
        self.covers: list[tuple[Any, Any]] = []
        self.reorders: list[Any] = []

    def create(self, album):
        from albuswall.dto.album.query import Album

        self.created.append(album)
        return Album(
            uuid=uuid4(),
            title=album.title,
            description=album.description,
            cover=None,
            type=album.album_type,
        )

    def update(self, album_uuid, patch):
        self.updates.append((album_uuid, patch))
        return True

    def rename(self, album_uuid, title):
        return self.update(album_uuid, title)

    def set_cover(self, album_uuid, cover_uuid):
        self.covers.append((album_uuid, cover_uuid))
        return True

    def reorder(self, order):
        self.reorders.append(order)
        return len(order.album_uuids)

    def delete(self, album_uuid, *, cascade=False):
        self.deleted.append((album_uuid, cascade))
        return True

    def restore(self, album_uuid):
        self.restored.append(album_uuid)
        return True

    def count_members(self, album_uuid):
        return 0

    def exists(self, album_uuid, *, include_deleted=False):
        return True


class FakeTagService:
    """``tag_service`` 最小 stub。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any]] = []
        self.deleted: list[tuple[Any, bool]] = []
        self.moved: list[tuple[Any, Any]] = []

    def list_all(self):
        return []

    def list_tree(self, *, orphan_policy="hide"):
        return []

    def list_with_stats(self, *, include_subtree=False):
        return []

    def list_orphans(self):
        return []

    def get(self, uuid):
        return None

    def ancestors(self, uuid):
        return []

    def path_label(self, uuid):
        return ""

    def search(self, keyword, *, album_uuid=None, scope="active", limit=20):
        return []

    def asset_uuids(self, tag_uuid, *, scope="active", include_descendants=True):
        return []

    def create(self, name, parent_uuid=None, **kwargs):
        self.created.append((name, parent_uuid))
        return uuid4()

    def ensure_path(self, path):
        return uuid4()

    def rename(self, tag_uuid, name):
        return None

    def update(self, tag_uuid, **kwargs):
        return None

    def move(self, tag_uuid, new_parent_uuid):
        self.moved.append((tag_uuid, new_parent_uuid))

    def delete(self, tag_uuid, *, cascade=False):
        self.deleted.append((tag_uuid, cascade))

    def restore(self, tag_uuid):
        from albuswall.service.tag.service import RestoreResult

        return RestoreResult(tag=None, parent_visible=True)


class FakeAliasService:
    """``alias_service`` 最小 stub。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, Any, Any]] = []

    def list_of_parent(self, parent_uuid=None):
        return []

    def create(self, name, target_uuid, parent_uuid=None):
        self.created.append((name, target_uuid, parent_uuid))
        return uuid4()

    def resolve(self, alias_uuid):
        return uuid4()

    def delete(self, alias_uuid):
        return True


class FakeAssetOpsService:
    """``asset_ops_service`` 最小 stub：写调用记在 ``calls``。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def classify_assets(self, asset_uuids):
        from albuswall.dto.asset.events import ClassifiedAssets

        uuids = tuple(asset_uuids)
        return ClassifiedAssets(visible=uuids, deleted=(), missing=())

    def set_favorite(self, asset_uuid, value):
        from albuswall.dto.asset.events import FavoriteResult

        self.calls.append(("favorite", (asset_uuid, value)))
        return FavoriteResult(
            asset_uuid=asset_uuid, outcome="updated", previous_value=False,
        )

    def trash(self, asset_uuids):
        from albuswall.dto.asset.events import TrashResult

        uuids = tuple(asset_uuids)
        self.calls.append(("trash", uuids))
        return TrashResult(changed=uuids, already_deleted=(), missing=())

    def restore(self, asset_uuids):
        from albuswall.dto.asset.events import RestoreResult

        uuids = tuple(asset_uuids)
        self.calls.append(("restore", uuids))
        return RestoreResult(restored=uuids, not_found=(), conflicts=())

    def add_to_album(self, album_uuid, asset_uuids):
        from albuswall.dto.asset.events import MembershipResult

        uuids = tuple(asset_uuids)
        self.calls.append(("add", (album_uuid, uuids)))
        return MembershipResult(added=uuids)

    def remove_from_album(self, album_uuid, asset_uuids):
        from albuswall.dto.asset.events import MembershipResult

        uuids = tuple(asset_uuids)
        self.calls.append(("remove", (album_uuid, uuids)))
        return MembershipResult(removed=uuids)

    def move_between_albums(self, src, dst, asset_uuids):
        from albuswall.dto.asset.events import MoveResult

        uuids = tuple(asset_uuids)
        self.calls.append(("move", (src, dst, uuids)))
        return MoveResult(moved=uuids)

    def copy_into_album(self, album_uuid, asset_uuids):
        return self.add_to_album(album_uuid, asset_uuids)


class FakeAssetTagService:
    """``asset_tag_service`` 最小 stub。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def list_of_asset(self, asset_uuid):
        return []

    def add(self, asset_uuid, tag_uuids):
        from albuswall.service.asset.command.tagging import AddTagsResult

        self.calls.append(("add", (asset_uuid, tuple(tag_uuids))))
        return AddTagsResult(
            added=len(tuple(tag_uuids)), existing=0, skipped_uuids=(),
        )

    def remove(self, asset_uuid, tag_uuids):
        self.calls.append(("remove", (asset_uuid, tuple(tag_uuids))))
        return len(tuple(tag_uuids))

    def set(self, asset_uuid, tag_uuids):
        from albuswall.service.asset.command.tagging import SetTagsResult

        self.calls.append(("set", (asset_uuid, tuple(tag_uuids))))
        return SetTagsResult(
            added=0, removed=0, existing=0, skipped_uuids=(),
        )

    def count_for_tag(self, tag_uuid):
        return 0

    def count_for_tags(self, tag_uuids):
        return {str(u): 0 for u in tag_uuids}


class FakeTrashService:
    """``trash_service``（资产级）最小 stub。"""

    def __init__(self) -> None:
        self.empty_calls = 0
        self.cleanup_calls = 0

    def list_deleted(self, limit, offset):
        return []

    def count_deleted(self):
        return 0

    def get_deleted(self, asset_id):
        return None

    def get_state(self):
        from albuswall.dto.trash import TrashState

        return TrashState(
            last_observed_at="2026-01-01T00:00:00.000",
            last_cleanup_at=None,
            last_cleanup_deleted=0,
            suspicious_clock_jumps=0,
        )

    def restore_by_uuids(self, asset_uuids):
        from albuswall.dto.asset.events import RestoreResult

        return RestoreResult(
            restored=tuple(asset_uuids), not_found=(), conflicts=(),
        )

    def cleanup_expired(self):
        from albuswall.dto.trash import CleanupReport, ClockJump

        self.cleanup_calls += 1
        return CleanupReport(
            purged=[], clock_jump=ClockJump(kind="normal", advised_cutoff=None),
            state=self.get_state(), thumb_failures=[],
        )

    def empty_trash(self):
        from albuswall.dto.asset.events import PurgeReport

        self.empty_calls += 1
        return PurgeReport(
            purged=(), thumb_failures=(), requested=0, skipped=(),
            completed=True, error=None, count_after=0,
        )


class FakeTrashCleanupScheduler:
    def __init__(self) -> None:
        self.triggered = 0

    def trigger_now(self):
        self.triggered += 1
        return object()

    def describe(self, *, verbose=False):
        return "FakeTrashCleanupScheduler()"


class FakeThumbnailService:
    """``thumbnail_service`` 最小 stub（只覆盖命令层会调的读 / 请求面）。"""

    def __init__(self) -> None:
        self.requested: list[tuple[list[Any], bool]] = []
        self.regenerated: list[Any] = []

    def stats(self):
        return {"pending": 0}

    def describe(self, *, verbose=False):
        return "FakeThumbnailService()"

    def get_thumbnail_path(self, asset_uuid, spec=None):
        return None

    def get_thumbnail_paths(self, asset_uuids, spec=None):
        return {}

    def request_bulk(self, asset_uuids, *, include_deleted=False, limit=None):
        uuids = list(asset_uuids)
        self.requested.append((uuids, include_deleted))
        return len(uuids)

    def regenerate(self, uuid):
        self.regenerated.append(uuid)
        return object()

    def scan_and_submit(self, batch=None):
        return 0

    def run_once(self, batch=None):
        return {"scanned": 0, "ok": 0, "failed": 0, "skipped": 0}

    def purge(self, asset_uuid):
        return True

    def purge_bulk(self, asset_uuids):
        return len(list(asset_uuids))


class FakeThumbnailCleaner:
    def __init__(self) -> None:
        self.scanned = 0

    def describe(self, *, verbose=False):
        return "FakeThumbnailCleaner()"

    def scan(self):
        from albuswall.service.thumbnail.cleaner import OrphanCleanupReport

        self.scanned += 1
        return OrphanCleanupReport()

    def scan_and_purge(self):
        from albuswall.service.thumbnail.cleaner import OrphanCleanupReport

        self.scanned += 1
        return OrphanCleanupReport()


class FakeImportService:
    """``import_service`` 的最小 stub：记录 ``trigger()`` 次数。"""

    def __init__(self) -> None:
        self.triggers = 0

    def trigger(self) -> None:
        self.triggers += 1


class FakeSourceTrashService:
    """``source_trash_service`` 的最小 stub：记录回收站三件套的调用。

    另外提供 ``sources_changed`` —— 装配层（``_wire_source_trash``）会对它
    ``connect``，没有这个属性连 ``register_service`` 都跑不完。
    """

    def __init__(self) -> None:
        self.trashed: list[int] = []
        self.restored: list[int] = []
        self.purged: list[int] = []
        self.sources_changed = _NullSignal()

    def delete_source(self, source_id: int) -> bool:
        self.trashed.append(source_id)
        self.sources_changed.emit(
            self, source_id=source_id, kind="soft_delete",
        )
        return True

    def restore_source(self, source_id: int) -> bool:
        self.restored.append(source_id)
        self.sources_changed.emit(self, source_id=source_id, kind="restore")
        return True

    def purge_source_now(self, source_id: int):
        from albuswall.dto.source.events import SourcePurgeReport

        self.purged.append(source_id)
        return SourcePurgeReport(
            source_id=source_id, purged=True, asset_ids=[], asset_uuids=[],
        )


# ==================== fixture ====================

@pytest.fixture
def fresh_registry() -> CommandRegistry:
    """每个用例一张全新注册表，绕开进程全局单例与 freeze。"""
    return CommandRegistry()


@pytest.fixture
def fake_container() -> Container:
    """只装了 **service** stub 的容器（不含任何 repo）。

    handler 的 ``setup`` 用 ``container.require("xxx_service")`` 取依赖；
    容器里没有 ``view_repo`` 之类的 key —— 若某个 handler 误改为直调 repo，
    这里会直接 ``MissingDependencyError``，比断言更硬。
    """
    container = Container()
    container.set("task_service", FakeTaskService())
    container.set("source_service", FakeSourceService())
    container.set("source_state_service", FakeSourceStateService())
    container.set("source_runtime", FakeSourceRuntime())
    container.set("source_trash_service", FakeSourceTrashService())
    container.set("import_service", FakeImportService())
    container.set("view_service", FakeViewService())
    container.set("album_service", FakeAlbumService())
    container.set("tag_service", FakeTagService())
    container.set("alias_service", FakeAliasService())
    container.set("asset_ops_service", FakeAssetOpsService())
    container.set("asset_tag_service", FakeAssetTagService())
    container.set("trash_service", FakeTrashService())
    container.set("trash_cleanup_scheduler", FakeTrashCleanupScheduler())
    container.set("thumbnail_service", FakeThumbnailService())
    container.set("thumbnail_cleaner", FakeThumbnailCleaner())
    return container


@pytest.fixture
def assembled(fresh_registry, fake_container):
    """装配好全部 handler 的 (registry, container) —— 只注册，不 freeze。"""
    setup_handlers(fresh_registry, fake_container)
    return fresh_registry, fake_container


@pytest.fixture
def clean_hooks():
    """快照并恢复全局 hook 表：``register_service`` 会往里追加 on_loop。

    全局 ``hooks_map`` 是进程级的，用例之间不清会互相污染。
    """
    from albuswall.core.registry import hooks_map

    saved = {name: list(hooks) for name, hooks in hooks_map.items()}
    try:
        yield hooks_map
    finally:
        for name, hooks in saved.items():
            hooks_map[name][:] = hooks


# ==================== 全局状态复位 ====================

@pytest.fixture(autouse=True)
def _reset_global_plugin_state():
    """每个用例结束后复位进程级全局状态。

    插件系统把三处状态放在模块 / 类级（而不是实例上），它们天然跨用例泄漏：

    * ``core.registry.hooks_map``——插件 ``ctx.on(...)`` 会往里追加 hook；
    * ``core.plugins._pending``——``@plugin`` 的收集表，留着会让下个用例
      意外多出插件；
    * ``Application`` / ``CoreApplication`` / ``Configue`` 的单例字段。

    hook 用**原地切片赋值**恢复：``hooks_map[event]`` 这个 list 对象可能已经
    被别的模块持有引用（例如某个 hook 追加时拿到的就是它），换对象会让那些
    引用指向旧列表。
    """
    from albuswall.configue.configue import Configue
    from albuswall.core.application import Application, CoreApplication
    from albuswall.core.plugins import _pending
    from albuswall.core.registry import hooks_map

    hook_snapshot = {k: list(v) for k, v in hooks_map.items()}
    pending_snapshot = list(_pending)
    inst, self_, init = Application._instance, Application._self, Application._is_init
    reg = CoreApplication._hooks_registered
    cfg = Configue._instance

    yield

    for k, v in hook_snapshot.items():
        hooks_map[k][:] = v        # 原地恢复，不换 list 对象
    _pending[:] = pending_snapshot
    Application._instance, Application._self, Application._is_init = inst, self_, init
    CoreApplication._hooks_registered = reg
    Configue._instance = cfg
