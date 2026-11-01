#
"""``service/source/trash``：回收站服务与它的配置。

覆盖四类东西：

1. **配置**：``@configclass`` 声明默认值、值对象校验、``Configue`` 缺席时的
   回退（回退必须记 WARNING，不能静默）；
2. **软删 / 恢复**：写库 → 投递 purge → 发信号，三种 ``kind`` 的 payload 形状；
3. **异步 purge**：Reconciler 契约（``incremental`` / ``full``）的适配、
   key 解析、单源失败不中断整批、启动补清理、purged 只在真的删掉后才发；
4. **磁盘侧**：共享 md5 布局跳过、XDG 防护、旧私有布局按 base/rel 删。

线程边界单独测（``_FakeMainLoop``）：跨线程调用必须经主循环，而在工作线程
里跑的 purge 回调**不得**再往主循环搬。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pytest

from albuswall.dto.thumbnail.paths import ThumbnailPaths
from albuswall.service.source.errors import SourceVirtualRoot
from albuswall.service.source.trash import (
    SourceTrashConfig,
    SourceTrashService,
    load_source_trash_config,
    source_trash_config,
)

# =========================================================================== #
# 替身
# =========================================================================== #

MANUAL = 0


class _FakeSourceRepo:
    """``IngestSourceRepository`` 的回收站面：软删 / 恢复 / 硬删 / 待删清单。"""

    def __init__(self, ids: tuple[int, ...] = (1,)) -> None:
        self.rows: set[int] = set(ids)
        self.deleted: set[int] = set()
        self.pending: list[int] = []
        self.calls: list[str] = []
        #: 每个源 purge 时报多少资产行（默认 0）
        self.assets: dict[int, int] = {}
        #: 让 purge 抛异常，验证「单源失败不中断整批」
        self.explode_on: set[int] = set()

    # ---- 写 ----
    def soft_delete(self, source_id: int) -> bool:
        self.calls.append(f"soft_delete:{source_id}")
        if source_id == MANUAL or source_id not in self.rows:
            return False
        if source_id in self.deleted:
            return False
        self.deleted.add(source_id)
        self.pending.append(source_id)
        return True

    def restore(self, source_id: int) -> bool:
        self.calls.append(f"restore:{source_id}")
        if source_id not in self.deleted:
            return False
        self.deleted.discard(source_id)
        if source_id in self.pending:
            self.pending.remove(source_id)
        return True

    def purge(self, source_id: int):
        from albuswall.dto.source.events import SourcePurgeReport

        self.calls.append(f"purge:{source_id}")
        if source_id in self.explode_on:
            raise RuntimeError(f"boom {source_id}")
        if source_id not in self.deleted:
            return SourcePurgeReport(
                source_id=source_id, purged=False,
                asset_ids=[], asset_uuids=[],
            )
        count = self.assets.get(source_id, 0)
        self.deleted.discard(source_id)
        self.rows.discard(source_id)
        if source_id in self.pending:
            self.pending.remove(source_id)
        return SourcePurgeReport(
            source_id=source_id,
            purged=True,
            asset_ids=list(range(count)),
            asset_uuids=[uuid4() for _ in range(count)],
        )

    # ---- 读 ----
    def list_pending_purge_ids(self) -> list[int]:
        self.calls.append("list_pending")
        return list(self.pending)


class _FakeThumbRepo:
    """``ThumbnailRepository.get_paths_by_source`` 的替身。"""

    def __init__(
            self,
            paths: Optional[dict[int, dict[str, Any]]] = None,
            log: Optional[list[str]] = None,
    ) -> None:
        self._paths = paths or {}
        self._log = log
        self.asked: list[int] = []

    def get_paths_by_source(self, source_id: int) -> dict[str, Any]:
        self.asked.append(source_id)
        if self._log is not None:
            self._log.append(f"thumbs:{source_id}")
        return self._paths.get(source_id, {})


class _FakeReconciler:
    """``Reconciler`` 的替身：记录注册与投递，不涉及线程。"""

    def __init__(self, *, explode: bool = False) -> None:
        self.registered: list[dict[str, Any]] = []
        self.invalidated: list[tuple[str, str]] = []
        self._explode = explode

    def register(self, name: str, **kwargs: Any) -> None:
        self.registered.append({"name": name, **kwargs})

    def invalidate(self, name: str, key: str) -> None:
        if self._explode:
            raise KeyError(f"unknown reconciler: {name!r}")
        self.invalidated.append((name, key))


class _FakeMainLoop:
    """记录投递并**就地执行**（等价于"已在该线程"的快路径）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, fn):
        self.calls.append("call")
        return fn()


def _service(
        *,
        repo: Optional[_FakeSourceRepo] = None,
        thumbs: Optional[_FakeThumbRepo] = None,
        reconciler: Optional[Any] = None,
        main_loop: Optional[Any] = None,
        **config: Any,
) -> tuple[SourceTrashService, _FakeSourceRepo, _FakeThumbRepo, Any]:
    repo = repo or _FakeSourceRepo()
    thumbs = thumbs or _FakeThumbRepo()
    reconciler = reconciler if reconciler is not None else _FakeReconciler()
    service = SourceTrashService(
        repo, thumbs, reconciler,
        SourceTrashConfig(**config),
        main_loop=main_loop,
    )
    return service, repo, thumbs, reconciler


# =========================================================================== #
# 1. 配置
# =========================================================================== #

def test_declared_defaults_match_the_value_object():
    """``@configclass`` 的声明默认值必须与值对象一致（不能各写一份）。

    ``[source_trash]`` 段由两个域**共享**：本域只声明三个键，缩略图维护的四个
    键在 ``service/thumbnail/cleaner.py`` 的 ``CleanerSettings`` 里（同一
    section 的多份声明共享一个 Resolver 节点，见 configue 的「声明共享」）。
    """
    snapshot = source_trash_config()

    assert snapshot == {
        "reconciler_name": "source_trash",
        "priority": 8,
        "debounce": 1.0,
    }, "本域只该看到自己声明的键"
    assert load_source_trash_config(snapshot) == SourceTrashConfig()


def test_shared_section_is_split_between_domains():
    """同段两域各自声明互不覆盖，且缩略图侧能从同一段读到自己的键。"""
    from albuswall.service.thumbnail.cleaner import (
        CleanerSettings,
        _cleaner_settings,
    )

    assert set(CleanerSettings.__dict__) & {
        "xdg_compat", "orphan_min_age_seconds",
        "session_marker_path", "auto_cleanup_on_dirty",
    }, "缩略图侧应自己声明这四个键"

    # 两边读的是同一段、不同键
    assert set(_cleaner_settings()) == {
        "xdg_compat", "orphan_min_age_seconds",
        "session_marker_path", "auto_cleanup_on_dirty",
    }
    assert set(source_trash_config()) == {
        "reconciler_name", "priority", "debounce",
    }


def test_config_rejects_invalid_values():
    with pytest.raises(ValueError, match="debounce"):
        SourceTrashConfig(debounce=-0.1)
    with pytest.raises(ValueError, match="reconciler_name"):
        SourceTrashConfig(reconciler_name="")


def test_config_falls_back_with_a_warning_outside_a_config_system(caplog):
    """没有 ``Configue`` 时用声明默认值，并**记一条 WARNING**（不静默）。"""
    from albuswall.configue.configue import Configue

    saved = Configue._instance
    Configue._instance = None
    try:
        with caplog.at_level("WARNING"):
            snapshot = source_trash_config()
    finally:
        Configue._instance = saved

    assert snapshot == {
        "reconciler_name": "source_trash", "priority": 8, "debounce": 1.0,
    }
    assert "falling back to declared defaults" in caplog.text


# =========================================================================== #
# 2. 构造：Reconciler 注册
# =========================================================================== #

def test_constructor_registers_a_startup_reconciler():
    _service(reconciler=None)
    service, _repo, _thumbs, reconciler = _service()

    (entry,) = reconciler.registered
    assert entry["name"] == "source_trash"
    assert entry["on_startup"] is True, "启动补清理依赖它"
    assert entry["debounce"] == 1.0
    assert entry["priority"] == 8
    # 两个回调必须是本服务的真实方法（不是 lambda：purge 要能被单测直接调）
    assert entry["incremental"] == service._purge_batch
    assert entry["full"] == service._purge_all_pending


def test_describe_is_human_readable():
    service, *_ = _service()
    assert "source_trash" in service.describe()
    assert "Fake" in service.describe(verbose=True)


# =========================================================================== #
# 3. 软删 / 恢复
# =========================================================================== #

def test_delete_source_soft_deletes_schedules_and_emits():
    service, repo, _thumbs, reconciler = _service()
    seen: list[dict[str, Any]] = []
    service.sources_changed.connect(lambda _s, **kw: seen.append(kw))

    assert service.delete_source(1) is True

    assert 1 in repo.deleted
    assert reconciler.invalidated == [("source_trash", "1")]
    assert seen == [{"source_id": 1, "kind": "soft_delete"}]


def test_delete_source_is_idempotent_and_silent_when_nothing_changed():
    service, repo, _thumbs, reconciler = _service()
    seen: list[dict[str, Any]] = []
    service.sources_changed.connect(lambda _s, **kw: seen.append(kw))

    repo.deleted.add(1)  # 已经是删除态

    assert service.delete_source(1) is False
    assert reconciler.invalidated == [], "没软删成功就不该投递 purge"
    assert seen == []


def test_delete_manual_source_raises_the_shared_error_family():
    """虚拟根抛 ``SourceVirtualRoot`` —— 与 ``SourceService.delete`` 同一个类型。"""
    service, _repo, _thumbs, reconciler = _service()

    with pytest.raises(SourceVirtualRoot):
        service.delete_source(MANUAL)

    assert reconciler.invalidated == []


def test_restore_source_undoes_the_soft_delete():
    service, repo, _thumbs, _reconciler = _service()
    seen: list[dict[str, Any]] = []
    service.sources_changed.connect(lambda _s, **kw: seen.append(kw))

    service.delete_source(1)
    assert service.restore_source(1) is True

    assert 1 not in repo.deleted
    assert seen[-1] == {"source_id": 1, "kind": "restore"}
    # 恢复之后 purge 整个空转（repo 层的 ``is_deleted=1`` 守卫）
    assert repo.purge(1).purged is False


def test_restore_returns_false_when_already_purged():
    service, repo, _thumbs, _reconciler = _service()
    service.delete_source(1)
    service.purge_source_now(1)          # 硬删走完

    assert service.restore_source(1) is False


def test_scheduling_failure_does_not_break_the_delete(monkeypatch):
    """Reconciler 投递失败只记日志：软删已经提交，删除对用户就是成功的。"""
    service, repo, _thumbs, _reconciler = _service()

    class _Broken:
        def register(self, name: str, **kwargs: Any) -> None:
            pass

        def invalidate(self, name: str, key: str) -> None:
            raise RuntimeError("reconciler is gone")

    service._reconciler = _Broken()

    assert service.delete_source(1) is True
    assert 1 in repo.deleted


# =========================================================================== #
# 4. 异步 purge（Reconciler 两个回调）
# =========================================================================== #

def test_purge_batch_purges_every_key():
    service, repo, _thumbs, _reconciler = _service()
    service.delete_source(1)
    service.delete_source(2)

    service._purge_batch({"1", "2"})

    assert 1 not in repo.rows and 2 not in repo.rows


def test_purge_batch_ignores_malformed_keys():
    service, repo, _thumbs, _reconciler = _service()
    service.delete_source(1)

    service._purge_batch({"1", "not-an-int", ""})

    assert 1 not in repo.rows


def test_purge_batch_isolates_a_failing_source():
    """一个源炸了不能让整批停下（剩下的源不该被永久挂在 pending 里）。"""
    service, repo, _thumbs, _reconciler = _service()
    service.delete_source(1)
    service.delete_source(2)
    repo.explode_on.add(1)

    service._purge_batch({"1", "2"})

    assert 1 in repo.deleted, "失败的那个仍未 purge"
    assert 2 not in repo.rows, "同一批里的另一个必须照常删掉"


def test_purge_all_pending_is_the_startup_catch_up():
    service, repo, _thumbs, _reconciler = _service()
    repo.deleted.add(1)
    repo.pending.append(1)               # 上次退出时软删了但没 purge

    service._purge_all_pending()

    assert repo.rows == set()


def test_purge_all_pending_without_pending_rows_does_nothing():
    service, repo, _thumbs, _reconciler = _service()
    repo.calls.clear()

    service._purge_all_pending()

    assert repo.calls == ["list_pending"]


def test_purge_emits_only_when_the_row_really_disappeared():
    service, repo, _thumbs, _reconciler = _service()
    seen: list[dict[str, Any]] = []
    service.sources_changed.connect(lambda _s, **kw: seen.append(kw))

    # 没软删 → purge 空转 → 不发信号
    assert service.purge_source_now(1).purged is False
    assert seen == []

    service.delete_source(1)
    assert service.purge_source_now(1).purged is True
    assert seen[-1] == {"source_id": 1, "kind": "purged"}


def test_thumbnail_paths_are_snapshotted_before_purge():
    """必须先快照再删：purge 之后 assets 行没了，再也查不到 thumb_path。"""
    order: list[str] = []
    repo = _FakeSourceRepo()
    repo.calls = order                      # 共用同一份有序日志
    thumbs = _FakeThumbRepo(log=order)
    service, _repo, _thumbs, _reconciler = _service(repo=repo, thumbs=thumbs)
    service.delete_source(1)
    order.clear()

    service.purge_source_now(1)

    assert order == ["thumbs:1", "purge:1"], "顺序反了就会把缩略图路径弄丢"


# =========================================================================== #
# 5. 磁盘侧：三种布局 / 防护
# =========================================================================== #

def _shared_layout_paths(root: Path, hex_name: str = "a" * 32) -> ThumbnailPaths:
    """新的共享 md5 布局：``<root>/<spec>/<md5>.png``。"""
    return ThumbnailPaths(
        base=str(root),
        small=f"normal/{hex_name}.png",
        medium=f"x-large/{hex_name}.png",
        large=f"xx-large/{hex_name}.png",
    )


def test_shared_md5_layout_is_recognised():
    assert SourceTrashService._uses_shared_md5_layout(
        _shared_layout_paths(Path("/tmp/x"))
    )


def test_legacy_private_layout_is_not_mistaken_for_shared():
    legacy = ThumbnailPaths(
        base="/tmp/x", small="small.jpg", medium="medium.jpg", large="large.jpg",
    )
    assert not SourceTrashService._uses_shared_md5_layout(legacy)


def test_shared_layout_files_are_left_to_the_cleaner(tmp_path):
    """共享 PNG 可能被多条 asset 行引用 —— purge 不得 unlink 它。"""
    shared = tmp_path / "normal" / ("b" * 32 + ".png")
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"png")
    tp = ThumbnailPaths(
        base=str(tmp_path), small=f"normal/{shared.name}",
        medium=None, large=None,
    )
    service, _repo, _thumbs, _reconciler = _service(
        thumbs=_FakeThumbRepo(paths={1: {"u1": tp}})
    )
    service.delete_source(1)

    service.purge_source_now(1)

    assert shared.exists(), "共享布局必须交给 ThumbnailCleaner，不能在 purge 里删"


def test_legacy_layout_files_are_removed(tmp_path):
    """旧私有布局（``base/{small,medium,large}.jpg``）仍按 base/rel 删。"""
    base = tmp_path / "assets" / "u1"
    base.mkdir(parents=True)
    for name in ("small.jpg", "medium.jpg"):
        (base / name).write_bytes(b"jpg")
    tp = ThumbnailPaths(
        base=str(base), small="small.jpg", medium="medium.jpg", large=None,
    )
    service, _repo, _thumbs, _reconciler = _service(
        thumbs=_FakeThumbRepo(paths={1: {"u1": tp}})
    )
    service.delete_source(1)

    service.purge_source_now(1)

    assert not base.exists(), "旧布局的文件与空目录都该被清掉"


def test_xdg_cache_is_never_deleted(tmp_path, monkeypatch):
    """``base`` 落在 XDG 缓存里说明 thumb_root 配错了：拒绝删除并记 warning。"""
    from albuswall.utils import xdg_thumbnail

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    root = xdg_thumbnail.xdg_thumbnails_root()
    victim = root / "normal" / "c.jpg"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"jpg")

    tp = ThumbnailPaths(base=str(root), small="normal/c.jpg", medium=None,
                        large=None)
    service, _repo, _thumbs, _reconciler = _service(
        thumbs=_FakeThumbRepo(paths={1: {"u1": tp}})
    )
    service.delete_source(1)

    service.purge_source_now(1)

    assert victim.exists(), "XDG 共享缓存不得被应用删除"
    assert os.path.isdir(root)


def test_empty_thumb_snapshot_is_a_no_op():
    service, _repo, _thumbs, _reconciler = _service()   # 快照为空
    service.delete_source(1)

    service.purge_source_now(1)     # 不抛、不崩


# =========================================================================== #
# 6. 线程边界
# =========================================================================== #

def test_write_entries_hop_to_the_main_loop():
    loop = _FakeMainLoop()
    service, repo, _thumbs, _reconciler = _service(main_loop=loop)

    service.delete_source(1)
    service.restore_source(1)

    assert loop.calls == ["call", "call"]
    assert "soft_delete:1" in repo.calls
    assert "restore:1" in repo.calls


def test_purge_callbacks_run_off_the_main_loop():
    """purge 跑在 TaskService 工作线程：绝不能再往主循环搬（会卡死队首）。"""
    loop = _FakeMainLoop()
    service, repo, _thumbs, _reconciler = _service(main_loop=loop)
    service.delete_source(1)          # 一次 hop
    loop.calls.clear()

    service._purge_batch({"1"})
    service._purge_all_pending()

    assert loop.calls == [], "purge 回调不得回主线程"
    assert repo.rows == set()


def test_without_a_main_loop_everything_runs_inline():
    service, repo, _thumbs, _reconciler = _service(main_loop=None)

    assert service.delete_source(1) is True

    assert 1 in repo.deleted


# =========================================================================== #
# 7. 真 Reconciler：debounce → purge 端到端
# =========================================================================== #

class _DeferredTasks:
    """``TaskService`` 的最小子集：只在 ``flush()`` 时执行（避免竞态）。

    ``Future`` 必须真的被 ``set_result``：Reconciler 靠 ``_on_done`` 收尾，
    在途不完成的 Future 会让后续 ``invalidate`` 一直挂在 pending 上。
    """

    def __init__(self) -> None:
        self.queued: list[tuple[Any, tuple, Any]] = []

    def submit(self, fn, *args, priority: Optional[int] = None):
        from concurrent.futures import Future

        fut: Any = Future()
        self.queued.append((fn, args, fut))
        return fut

    def flush(self) -> int:
        queued, self.queued = self.queued, []
        for fn, args, fut in queued:
            try:
                fut.set_result(fn(*args))
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
        return len(queued)

    def flush_until_idle(self) -> int:
        total = 0
        while self.queued:
            total += self.flush()
        return total


def test_real_reconciler_purges_after_debounce():
    import time as _time

    from albuswall.infrastructure.reconcile.service import Reconciler

    tasks = _DeferredTasks()
    reconciler = Reconciler(tasks)

    service, repo, _thumbs, _reconciler = _service(
        reconciler=reconciler, debounce=0.0,
    )
    reconciler.start()
    tasks.flush()                     # 启动补清理（此时没有 pending）

    service.delete_source(1)
    assert 1 in repo.rows, "debounce 期间不该已经删除"

    deadline = _time.monotonic() + 5.0
    while not tasks.queued and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert tasks.queued, "debounce 到期后应向 TaskService 提交 purge"

    tasks.flush_until_idle()

    assert 1 not in repo.rows, "purge 没生效"
    reconciler.shutdown()


# =========================================================================== #
# 8. 共享 section：两个域各自声明、各读各的键（真配置文件）
# =========================================================================== #

_SHARED_INI = """\
[source_trash]
reconciler_name = my_trash
priority = 3
debounce = 0.25
orphan_min_age_seconds = 42.0
auto_cleanup_on_dirty = false
"""


def test_both_domains_read_their_own_keys_from_one_section(tmp_path):
    """``[source_trash]`` 被两个 ``@configclass`` 共享：各读各的键，互不覆盖。

    这条钉住的是 configue 的「声明共享」语义在本仓库的实际用法：源回收站只
    声明 3 个键、缩略图维护声明 4 个键；同一个 ini 段里的值两边都能拿到。
    """
    from albuswall.configue import Configue, configclass
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.service.source.trash.config import SourceTrashSettings
    from albuswall.service.thumbnail.cleaner import (
        CleanerSettings,
        _cleaner_settings,
    )

    # tests/test_configue 的 autouse fixture 会清空登记表，这里幂等重建
    configclass("source_trash", "main")(SourceTrashSettings)
    configclass("source_trash", "main")(CleanerSettings)

    base = tmp_path / "cfg"
    base.mkdir()
    (base / "config.ini").write_text(_SHARED_INI, encoding="utf-8")

    previous = Configue._instance
    setup_config(
        Container(), base_dir=base, file_name="config.ini", portable=True,
    )
    try:
        trash_snapshot = source_trash_config()
        cleaner_snapshot = _cleaner_settings()
    finally:
        Configue._instance = previous

    assert trash_snapshot == {
        "reconciler_name": "my_trash", "priority": 3, "debounce": 0.25,
    }
    assert cleaner_snapshot["orphan_min_age_seconds"] == 42.0
    assert cleaner_snapshot["auto_cleanup_on_dirty"] is False
    # 两边看到的是**不同的键集合**（共享节点 ≠ 共享字段表）
    assert set(trash_snapshot).isdisjoint(
        {"orphan_min_age_seconds", "auto_cleanup_on_dirty"}
    )
