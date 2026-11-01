#
"""``service/source`` 四层拆分的行为与结构契约。

对应规格书 §10 的验收清单：

* 四个服务可从包根导入；异常家族是 ``ServiceError`` 子类；
* 三层（command / runtime / trigger）之间**没有** import 环；
* ``SourceRuntime.reconcile`` 四个分支各一条用例；
* 写路径信号 → state 跟踪集 → runtime 任务 → ``TaskService.submit``；
* ``disable`` / 回收站软删 / ``trigger_refresh_signal`` 的联动；
* ``scan_finished`` 仍能把"扫描完成"送到下游；
* ``register_service`` 的 boot / exit 钩子顺序。

一律用内存替身（fake repo / fake TaskService / fake facade），
不连真 DB、不起线程。
"""

from __future__ import annotations

import ast
import inspect
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Optional

import pytest

from albuswall.common.enums import FileTypeCheckMode
from albuswall.dto.source.command import (
    IngestSourceCreate,
    IngestSourceUpdate,
)
from albuswall.dto.source.events import SourceScanFinished
from albuswall.dto.source.query import MANUAL_SOURCE_ID, IngestSource
from albuswall.dto.trigger import TriggerConfig
from albuswall.service import Services, register_service
from albuswall.service.errors import ServiceError
from albuswall.service.source import (
    SourceCreateFailed,
    SourceDeleteFailed,
    SourceError,
    SourceMountFailed,
    SourceNotFound,
    SourcePathInvalid,
    SourceRuntime,
    SourceService,
    SourceServiceClosed,
    SourceStateService,
    SourceTriggerService,
    SourceUpdateFailed,
    SourceVirtualRoot,
)
from albuswall.utils.signals import Signal
from tests.conftest import FakeSourceTrashService

_SOURCE_PKG = Path(
    inspect.getfile(__import__("albuswall.service.source", fromlist=["x"]))
).parent


# =========================================================================== #
# 替身
# =========================================================================== #

class FakeIngestSourceRepository:
    """内存版 ``IngestSourceRepository``（只实现本批服务用到的接口）。"""

    def __init__(self) -> None:
        self.rows: dict[int, IngestSource] = {}
        self.deleted: set[int] = set()
        self.asset_counts: dict[int, int] = {}
        self._next_id = 1
        self.calls: list[str] = []
        self.trigger_configs: dict[int, Optional[TriggerConfig]] = {}

    # ---- 读 ----
    def _visible(self, source_id: int) -> Optional[IngestSource]:
        if source_id in self.deleted:
            return None
        return self.rows.get(source_id)

    def get_source_candidates(self) -> list[IngestSource]:
        return [s for sid, s in self.rows.items() if sid not in self.deleted]

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        if source_id == MANUAL_SOURCE_ID:
            return None
        return self._visible(source_id)

    def list_view_dtos(self) -> list[IngestSource]:
        return list(self.rows.values())

    def get_view_dto(self, source_id: int) -> Optional[IngestSource]:
        return self._visible(source_id)

    def exists(self, source_id: int, *, include_deleted: bool = False) -> bool:
        if include_deleted:
            return source_id in self.rows
        return self._visible(source_id) is not None

    def count_assets(self, source_id: int) -> int:
        return self.asset_counts.get(source_id, 0)

    def get_all_trigger_configs(self) -> list[TriggerConfig]:
        return [c for c in self.trigger_configs.values() if c is not None]

    def get_trigger_config(self, source_id: int) -> Optional[TriggerConfig]:
        return self.trigger_configs.get(source_id)

    # ---- 写 ----
    def create(self, source: IngestSourceCreate) -> int:
        new_id = self._next_id
        self._next_id += 1
        self.rows[new_id] = IngestSource(
            id=new_id,
            title=source.title,
            source_path=source.source_path,
            target_path=source.target_path,
            mount_point=source.mount_point,
            auto_mount=source.auto_mount,
            file_type_check=source.file_type_check,
            file_types=list(source.file_types),
            disabled=source.disabled,
        )
        self.calls.append(f"create:{new_id}")
        return new_id

    def update(self, source_id: int, patch: IngestSourceUpdate) -> bool:
        row = self.rows.get(source_id)
        if row is None or patch.is_empty():
            return False
        from dataclasses import replace

        from albuswall.dto.sentinel import UNSET

        changes: dict[str, Any] = {}
        for name in patch.__dataclass_fields__:
            value = getattr(patch, name)
            if value is UNSET:
                continue
            changes[name] = value
        # 真实仓储每次读都构造新 DTO；替身必须照做，否则调用方拿到的
        # "旧快照"与"新读取"是同一个对象，_source_changed 永远为假。
        self.rows[source_id] = replace(row, **changes)
        self.calls.append(f"update:{source_id}")
        return True

    def soft_delete(self, source_id: int) -> bool:
        if source_id == MANUAL_SOURCE_ID or source_id in self.deleted:
            return False
        if source_id not in self.rows:
            return False
        self.deleted.add(source_id)
        self.calls.append(f"soft_delete:{source_id}")
        return True

    def restore(self, source_id: int) -> bool:
        if source_id not in self.deleted:
            return False
        self.deleted.discard(source_id)
        self.calls.append(f"restore:{source_id}")
        return True

    def purge(self, source_id: int) -> Any:
        from albuswall.dto.source.events import SourcePurgeReport

        if source_id not in self.deleted:
            return SourcePurgeReport(
                source_id=source_id, purged=False,
                asset_ids=[], asset_uuids=[],
            )
        self.deleted.discard(source_id)
        self.rows.pop(source_id, None)
        self.calls.append(f"purge:{source_id}")
        return SourcePurgeReport(
            source_id=source_id, purged=True, asset_ids=[], asset_uuids=[],
        )

    # ---- 测试辅助 ----
    def put(self, source_id: int, **kwargs: Any) -> IngestSource:
        """直接放一行（跳过 create 的 DTO 校验）。"""
        defaults: dict[str, Any] = dict(
            title=f"源{source_id}",
            source_path="/tmp/nowhere",
            target_path=None,
            mount_point=None,
            auto_mount=False,
            file_type_check=FileTypeCheckMode.SUFFIX,
            file_types=["png"],
        )
        defaults.update(kwargs)
        row = IngestSource(id=source_id, **defaults)
        self.rows[source_id] = row
        return row


class FakeImportRepository:
    """内存版 ``ImportRepository``：候选缓存就是一个 path 集合。"""

    def __init__(self) -> None:
        self.cached: dict[int, list[str]] = {}

    def find_missing_paths(self, source_id: int, *paths: str) -> list[str]:
        known = set(self.cached.get(source_id, ()))
        return [p for p in paths if p not in known]

    def create_candidate_cache_batch(self, candidates: Any) -> int:
        for candidate in candidates:
            self.cached.setdefault(candidate.source_id, []).append(candidate.path)
        return len(candidates)


class FakeTaskService:
    """记录提交的 ``TaskService`` 替身。"""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.futures: list[Future] = []

    def submit(self, fn: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        self.submissions.append(
            {"fn": fn, "correlation_id": kwargs.get("correlation_id")}
        )
        self.futures.append(future)
        return future

    def settle(self, index: int = -1) -> None:
        """让第 index 个 future 完成（会触发 runtime 的 done 回调）。"""
        self.futures[index].set_result(None)

    @property
    def submitted_seqs(self) -> list[Any]:
        return [s["correlation_id"] for s in self.submissions]


class FakeFacade:
    """记录 upsert / remove 的 ``TriggerFacadeService`` 替身。"""

    def __init__(self) -> None:
        self.started_with: Optional[dict[int, TriggerConfig]] = None
        self.upserted: list[tuple[int, Any]] = []
        self.removed: list[int] = []

    def start(self, configs: dict[int, TriggerConfig]) -> None:
        self.started_with = configs

    def upsert(self, key: int, config: Any) -> None:
        self.upserted.append((key, config))

    def remove(self, key: int) -> None:
        self.removed.append(key)


class _Lookup:
    """最小的 ``Container.get`` 替身：装配层的分发器只用到 ``get``。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping

    def get(self, name: str) -> Any:
        return self._mapping[name]


class Harness:
    """四个服务 + 信号的完整装配（不含容器与钩子）。"""

    def __init__(self) -> None:
        self.repo = FakeIngestSourceRepository()
        self.import_repo = FakeImportRepository()
        self.task_service = FakeTaskService()
        self.facade = FakeFacade()
        self.trash_changed = Signal("SourceTrashChanged")

        self.command = SourceService(self.repo)
        self.runtime = SourceRuntime(
            self.import_repo, self.task_service, self.repo
        )
        self.state = SourceStateService(
            self.repo,
            self.runtime,
            source_created=self.command.source_created,
            source_changed=self.command.source_changed,
            source_deleted=self.command.source_deleted,
            trash_changed=self.trash_changed,
        )
        self.trigger = SourceTriggerService(
            self.repo,
            self.facade,
            source_created=self.command.source_created,
            source_changed=self.command.source_changed,
            source_deleted=self.command.source_deleted,
        )

    def _wire_trash(self) -> None:
        """复用装配层真正的分发器（软删 / 恢复 / purged 三分支）。"""
        from albuswall.service import _dispatch_trash_change

        lookup = _Lookup({
            "source_state_service": self.state,
            "source_trigger_service": self.trigger,
        })
        self.trash_changed.connect(_dispatch_trash_change(lookup))

    def start(self) -> "Harness":
        self.runtime.start()
        self.state.start()
        self.trigger.start()
        self._wire_trash()
        return self

    def stop(self) -> None:
        self.trigger.stop()
        self.state.shutdown()
        self.runtime.shutdown()


@pytest.fixture
def harness() -> Harness:
    h = Harness().start()
    try:
        yield h
    finally:
        h.stop()


def _make_source_create(path: str = "/tmp/nowhere") -> IngestSourceCreate:
    return IngestSourceCreate(title="源", source_path=path, file_types=["png"])


# =========================================================================== #
# 结构：包导出、异常家族、无环
# =========================================================================== #

def test_package_reexports_four_services():
    import albuswall.service.source as pkg

    for name in (
            "SourceService", "SourceStateService",
            "SourceRuntime", "SourceTriggerService",
    ):
        assert name in pkg.__all__
        assert getattr(pkg, name) is not None


def test_error_family_is_service_error_subtree():
    for exc in (
            SourceCreateFailed,
            SourceDeleteFailed,
            SourceMountFailed,
            SourceNotFound,
            SourcePathInvalid,
            SourceServiceClosed,
            SourceUpdateFailed,
            SourceVirtualRoot,
    ):
        assert issubclass(exc, SourceError), exc
        assert issubclass(exc, ServiceError), exc

    from albuswall.service.errors import AssetOpsError

    assert not issubclass(SourceError, AssetOpsError)
    assert not issubclass(AssetOpsError, SourceError)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
    return modules


def _relative_cross_imports(path: Path) -> set[str]:
    """``from ..runtime.service import X`` 这类**跨子包**的相对 import。

    返回值是目标子包名集合。豁免两项共享底座（它们不属于四层，谁都可以
    import，不构成层间耦合）：

    * ``errors``             —— 源域异常的唯一出口；
    * ``main_loop_service``  —— 写入口回主线程的通道（``command`` / ``state``
      都要用）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level < 2 or not node.module:
            continue
        head = node.module.split(".")[0]
        if head not in ("errors", "main_loop_service"):
            targets.add(head)
    return targets


def test_layers_do_not_import_each_other_cyclically():
    """command / runtime / trigger 三者互不 import；只有 state → runtime。"""
    forbidden: dict[str, tuple[str, ...]] = {
        # runtime 是"任务层"，本来就要跟 TaskService 说话；
        # command / state / trigger 都不得摸 TaskService。
        "command/service.py": ("runtime", "state", "trigger"),
        "runtime/service.py": ("state", "command", "trigger"),
        "runtime/executor.py": ("state", "command", "trigger", "runtime.service"),
        "trigger/service.py": ("state", "runtime", "command"),
        "state/service.py": ("command", "trigger"),
        # 回收站是第五层：谁也不 import，只跟 Reconciler / 两个仓储说话。
        "trash/service.py": ("command", "runtime", "state", "trigger"),
    }
    no_task_service = (
        "command/service.py", "trigger/service.py", "state/service.py",
    )
    for relative, banned in forbidden.items():
        modules = _imported_modules(_SOURCE_PKG / relative)
        for module in modules:
            for prefix in banned:
                assert not module.endswith("service.source." + prefix), (
                    f"{relative} 不该 import {module}"
                )
            if relative in no_task_service:
                assert "albuswall.infrastructure.task" not in module, (
                    f"{relative} 不该 import TaskService"
                )

    # 相对 import 也要守同一条边界（state → runtime 是**唯一**允许的跨层边）。
    allowed_cross = {
        "command/service.py": set(),
        "runtime/service.py": set(),
        "runtime/executor.py": set(),
        "trigger/service.py": set(),
        "state/service.py": {"runtime"},
        "state/tracking.py": set(),
        "trash/service.py": set(),
    }
    for relative, allowed in allowed_cross.items():
        crossing = _relative_cross_imports(_SOURCE_PKG / relative)
        assert crossing <= allowed, (
            f"{relative} 越界 import 了子包 {sorted(crossing - allowed)}"
        )


def test_command_service_has_no_runtime_state():
    """``SourceService`` 只依赖仓储：构造签名里没有 runtime / state / task。"""
    params = list(
        inspect.signature(SourceService.__init__).parameters
    )
    # ``main_loop`` 是 keyword-only 且默认 None：它是线程边界通道，不是本域
    # 的运行时依赖（详见 service/main_loop_service.py）。
    assert params == ["self", "source_repo", "main_loop"]
    main_loop = inspect.signature(SourceService.__init__).parameters["main_loop"]
    assert main_loop.kind is inspect.Parameter.KEYWORD_ONLY
    assert main_loop.default is None


# =========================================================================== #
# runtime：reconcile 四个分支
# =========================================================================== #

def _runtime() -> tuple[SourceRuntime, FakeTaskService, FakeIngestSourceRepository]:
    repo = FakeIngestSourceRepository()
    task_service = FakeTaskService()
    runtime = SourceRuntime(FakeImportRepository(), task_service, repo)
    runtime.start()
    return runtime, task_service, repo


def test_reconcile_added_creates_task_and_submits_once():
    runtime, task_service, repo = _runtime()
    source = repo.put(1)

    runtime.reconcile(added={1: source})

    assert runtime.has_task(1)
    assert len(runtime.tasks) == 1
    assert len(task_service.submissions) == 1
    assert task_service.submitted_seqs == [runtime.task_seqs[1]]

    runtime.shutdown()


def test_reconcile_rebuild_replaces_seq():
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    first_seq = runtime.task_seqs[1]

    runtime.reconcile(rebuild={1: repo.put(1, title="改过")})

    second_seq = runtime.task_seqs[1]
    assert second_seq != first_seq
    assert runtime.tasks[0].source.title == "改过"
    # 旧 seq 已被 prune 掉，只剩新 seq 在途
    assert runtime.submitted_seqs == {second_seq}
    assert first_seq not in runtime.submitted_seqs

    runtime.shutdown()


def test_reconcile_snapshot_updates_executor_in_place():
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    seq = runtime.task_seqs[1]
    executor = runtime.tasks[0]
    submissions = len(task_service.submissions)

    runtime.reconcile(snapshot={1: repo.put(1, title="新标题")})

    assert runtime.task_seqs[1] == seq, "snapshot 不该换 seq"
    assert runtime.tasks[0] is executor, "snapshot 不该换 executor"
    assert executor.source.title == "新标题"
    assert len(task_service.submissions) == submissions, "snapshot 不该重提"

    runtime.shutdown()


def test_reconcile_removed_drops_task():
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    runtime.reconcile(removed_ids=[1])

    assert runtime.tasks == []
    assert not runtime.has_task(1)
    assert runtime.submitted_seqs == set()

    runtime.shutdown()


def test_rescan_submits_new_seq_for_tracked_source():
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    seq = runtime.task_seqs[1]

    assert runtime.rescan(1) is True
    assert runtime.task_seqs[1] != seq
    assert runtime.task_seqs[1] in runtime.submitted_seqs

    assert runtime.rescan(404) is False

    runtime.shutdown()


def test_settled_task_is_not_resubmitted_by_unrelated_reconcile():
    """幂等派发：已扫完的任务不会因为一次无关的对账被重新激活。"""
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    first_seq = runtime.task_seqs[1]
    assert len(task_service.submissions) == 1

    task_service.settle()
    # 只有展示字段变化 → 走 snapshot 分支（不换 seq、不重提）
    runtime.reconcile(snapshot={1: repo.put(1, title="x")})

    assert len(task_service.submissions) == 1          # 没有新的提交
    assert runtime.task_seqs[1] == first_seq
    assert runtime.submitted_seqs == set()             # 在途已回收
    runtime.shutdown()


def test_rebuild_resubmits_with_new_seq():
    """源配置变了（rebuild）→ 换 seq 并重新派发（幂等的另一面）。"""
    runtime, task_service, repo = _runtime()
    runtime.reconcile(added={1: repo.put(1)})
    first_seq = runtime.task_seqs[1]
    task_service.settle()

    runtime.reconcile(rebuild={1: repo.put(1, source_path="/data/other")})

    assert len(task_service.submissions) == 2
    assert runtime.task_seqs[1] != first_seq
    assert runtime.submitted_seqs == {runtime.task_seqs[1]}
    runtime.shutdown()


def test_backpressure_skips_task_and_retries_next_round():
    """提交被背压拒绝：本轮不记在途，下一轮重试（异常不外抛）。"""
    from albuswall.common.exceptions import BackpressureError

    repo = FakeIngestSourceRepository()
    task_service = FakeTaskService()
    original = task_service.submit
    state = {"reject": True}

    def _submit(fn: Any, **kwargs: Any) -> Any:
        if state["reject"]:
            raise BackpressureError(reason="full", queue_size=8, max_queue_size=8)
        return original(fn, **kwargs)

    task_service.submit = _submit  # type: ignore[method-assign]
    runtime = SourceRuntime(FakeImportRepository(), task_service, repo)
    runtime.start()

    runtime.reconcile(added={1: repo.put(1)})
    assert runtime.has_task(1)
    assert runtime.submitted_seqs == set(), "被拒的 seq 不该记为在途"

    state["reject"] = False
    runtime.reconcile(snapshot={1: repo.put(1, title="重试")})
    assert len(task_service.submissions) == 1
    assert runtime.submitted_seqs == {runtime.task_seqs[1]}

    runtime.shutdown()


def test_task_service_shutdown_stops_the_round():
    """``TaskService`` 已关闭：记日志并 return，不抛给 UI 写路径。"""
    from albuswall.common.exceptions import TaskServiceShutdownError

    repo = FakeIngestSourceRepository()
    task_service = FakeTaskService()

    def _submit(fn: Any, **kwargs: Any) -> Any:
        raise TaskServiceShutdownError("closed")

    task_service.submit = _submit  # type: ignore[method-assign]
    runtime = SourceRuntime(FakeImportRepository(), task_service, repo)
    runtime.start()

    runtime.reconcile(added={1: repo.put(1), 2: repo.put(2)})

    assert runtime.submitted_seqs == set()
    assert len(runtime.tasks) == 2

    runtime.shutdown()


# =========================================================================== #
# 端到端：写命令 → state → runtime
# =========================================================================== #

def test_create_tracks_source_and_submits_scan(harness: Harness):
    new_id = harness.command.create(_make_source_create())

    assert new_id in harness.state.tracked_sources
    assert harness.runtime.has_task(new_id)
    assert len(harness.runtime.tasks) == 1
    assert len(harness.task_service.submissions) == 1


def test_create_pushes_trigger_config_to_facade(harness: Harness):
    new_id = harness.command.create(_make_source_create())

    assert [key for key, _ in harness.facade.upserted] == [new_id]


def test_disable_untracks_and_drops_task(harness: Harness):
    source_id = harness.command.create(_make_source_create())

    harness.command.disable(source_id)

    assert source_id not in harness.state.tracked_sources
    assert not harness.runtime.has_task(source_id)
    assert harness.runtime.tasks == []


def test_enable_after_disable_tracks_again(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    harness.command.disable(source_id)

    harness.command.enable(source_id)

    assert source_id in harness.state.tracked_sources
    assert harness.runtime.has_task(source_id)


def test_title_only_change_keeps_seq(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    seq = harness.runtime.task_seqs[source_id]

    harness.command.update(
        source_id, IngestSourceUpdate(title="只改标题")
    )

    assert harness.runtime.task_seqs[source_id] == seq
    assert harness.state.tracked_sources[source_id].title == "只改标题"


def test_scan_behaviour_change_rebuilds_seq(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    seq = harness.runtime.task_seqs[source_id]

    harness.command.update(
        source_id, IngestSourceUpdate(file_types=["png", "jpg"])
    )

    assert harness.runtime.task_seqs[source_id] != seq


def test_empty_patch_writes_nothing(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    before = list(harness.repo.calls)

    harness.command.update(source_id, IngestSourceUpdate())

    assert harness.repo.calls == before


def test_delete_emits_and_untracks(harness: Harness):
    source_id = harness.command.create(_make_source_create())

    harness.command.delete(source_id)

    assert not harness.repo.exists(source_id)
    assert source_id not in harness.state.tracked_sources
    assert not harness.runtime.has_task(source_id)
    assert harness.facade.removed == [source_id]


def test_delete_manual_source_raises_virtual_root(harness: Harness):
    with pytest.raises(SourceVirtualRoot):
        harness.command.delete(MANUAL_SOURCE_ID)


def test_delete_with_assets_raises(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    harness.repo.asset_counts[source_id] = 3

    with pytest.raises(SourceDeleteFailed):
        harness.command.delete(source_id)

    assert harness.repo.exists(source_id)


def test_shutdown_rejects_writes_but_keeps_reads():
    repo = FakeIngestSourceRepository()
    repo.put(1)
    service = SourceService(repo)
    service.shutdown()
    service.shutdown()  # 幂等

    with pytest.raises(SourceServiceClosed):
        service.create(_make_source_create())
    with pytest.raises(SourceServiceClosed):
        service.update(1, IngestSourceUpdate(title="x"))
    with pytest.raises(SourceServiceClosed):
        service.delete(1)

    # 读路径仍然可用
    assert service.has_source(1)
    assert service.get_source(1) is not None
    assert service.list_sources()


def test_create_rejects_empty_source_path():
    service = SourceService(FakeIngestSourceRepository())

    with pytest.raises(SourcePathInvalid):
        service.create(_make_source_create(path="  "))


def test_create_wraps_repository_failure():
    class Broken(FakeIngestSourceRepository):
        def create(self, source: IngestSourceCreate) -> int:
            raise RuntimeError("boom")

    service = SourceService(Broken())
    with pytest.raises(SourceCreateFailed) as info:
        service.create(_make_source_create())
    assert isinstance(info.value.cause, RuntimeError)


def test_update_missing_source_raises():
    service = SourceService(FakeIngestSourceRepository())

    with pytest.raises(SourceNotFound):
        service.update(404, IngestSourceUpdate(title="x"))


def test_update_not_written_raises_update_failed():
    class NoopUpdate(FakeIngestSourceRepository):
        def update(self, source_id: int, patch: IngestSourceUpdate) -> bool:
            return False

    repo = NoopUpdate()
    repo.put(1)
    service = SourceService(repo)

    with pytest.raises(SourceUpdateFailed):
        service.update(1, IngestSourceUpdate(title="x"))


# =========================================================================== #
# 回收站桥接与触发器到点
# =========================================================================== #

def test_trash_soft_delete_untracks_and_removes_trigger(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    harness.repo.soft_delete(source_id)

    harness.trash_changed.emit(None, source_id=source_id, kind="soft_delete")

    assert source_id not in harness.state.tracked_sources
    assert not harness.runtime.has_task(source_id)
    assert source_id in harness.facade.removed


def test_trash_restore_retracks_and_upserts_trigger(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    harness.repo.soft_delete(source_id)
    harness.trash_changed.emit(None, source_id=source_id, kind="soft_delete")
    harness.facade.upserted.clear()

    harness.repo.restore(source_id)
    harness.trash_changed.emit(None, source_id=source_id, kind="restore")

    assert source_id in harness.state.tracked_sources
    assert harness.runtime.has_task(source_id)
    assert [key for key, _ in harness.facade.upserted] == [source_id]


def test_trash_purged_event_is_ignored(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    tracked_before = dict(harness.state.tracked_sources)

    harness.trash_changed.emit(None, source_id=source_id, kind="purged")

    assert dict(harness.state.tracked_sources) == tracked_before
    assert harness.runtime.has_task(source_id)
    assert harness.facade.removed == []


def test_refresh_source_rescans_with_new_seq(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    seq = harness.runtime.task_seqs[source_id]

    harness.state.refresh_source(source_id)

    assert harness.runtime.task_seqs[source_id] != seq
    assert harness.runtime.task_seqs[source_id] in harness.runtime.submitted_seqs


def test_refresh_source_drops_vanished_source(harness: Harness):
    source_id = harness.command.create(_make_source_create())
    harness.repo.deleted.add(source_id)  # 行已不可见，但没走信号

    harness.state.refresh_source(source_id)

    assert source_id not in harness.state.tracked_sources
    assert not harness.runtime.has_task(source_id)


# =========================================================================== #
# 扫描完成 → 下游
# =========================================================================== #

def test_scan_finished_carries_payload_to_subscriber(harness: Harness, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    source_id = harness.command.create(_make_source_create(str(tmp_path)))
    received: list[SourceScanFinished] = []
    harness.runtime.scan_finished.connect(
        lambda _sender, *, event: received.append(event)
    )

    # 模拟 TaskService 真正执行任务：runtime 的 executor 就是那个 fn
    harness.runtime.tasks[0]()

    assert len(received) == 1
    event = received[0]
    assert event.source_id == source_id
    assert event.ok
    assert event.scanned_file_count == 1
    assert event.inserted_count == 1


def test_executor_skips_when_source_vanished(harness: Harness, tmp_path):
    source_id = harness.command.create(_make_source_create(str(tmp_path)))
    harness.repo.deleted.add(source_id)

    event = harness.runtime.tasks[0].run()

    assert event.skipped is True


# =========================================================================== #
# state 层的报错通道（SourceMountFailed / SourcePathInvalid / SourceNotFound）
# =========================================================================== #

def test_mount_source_raises_not_found(harness: Harness):
    with pytest.raises(SourceNotFound):
        harness.state.mount_source(404)


def test_mount_source_raises_invalid_path(harness: Harness):
    harness.repo.put(
        7, source_path="/definitely/not/here", auto_mount=True,
        mount_point="/mnt/x", target_path="//server/share",
    )

    with pytest.raises(SourcePathInvalid):
        harness.state.mount_source(7)


def test_mount_source_wraps_os_error(harness: Harness, tmp_path):
    harness.repo.put(
        8, source_path=str(tmp_path), auto_mount=True,
        mount_point=str(tmp_path / "mnt"), target_path="//server/share",
    )

    with pytest.raises(SourceMountFailed) as info:
        harness.state.mount_source(8)
    assert info.value.source_id == 8


def test_mount_failure_untracks_source(harness: Harness, tmp_path):
    """挂载失败必须连旧任务一起撤掉，不能留"挂了还在扫"。"""
    harness.repo.put(
        9, source_path=str(tmp_path), auto_mount=True,
        mount_point=str(tmp_path / "mnt"), target_path="//server/share",
    )

    harness.state.refresh_source(9)

    assert not harness.state.is_tracked(9)
    assert not harness.runtime.has_task(9)


# =========================================================================== #
# 触发器桥接
# =========================================================================== #

class _EnabledTriggerConfig:
    """``is_void()`` 为假的配置替身（默认 ``TriggerConfig()`` 是空配置）。"""

    def __init__(self, key: int) -> None:
        self.id = key

    def is_void(self) -> bool:
        return False


def test_trigger_start_loads_all_configs():
    repo = FakeIngestSourceRepository()
    repo.put(1)
    repo.trigger_configs[1] = _EnabledTriggerConfig(1)
    facade = FakeFacade()
    service = SourceTriggerService(
        repo, facade,
        source_created=Signal("a"), source_changed=Signal("b"),
        source_deleted=Signal("c"),
    )

    service.start()
    try:
        assert facade.started_with is not None
        assert 1 in facade.started_with
    finally:
        service.stop()


def test_trigger_on_source_upserted_raises_not_found():
    service = SourceTriggerService(
        FakeIngestSourceRepository(), FakeFacade(),
        source_created=Signal("a"), source_changed=Signal("b"),
        source_deleted=Signal("c"),
    )

    with pytest.raises(SourceNotFound):
        service.on_source_upserted(42)


def test_trigger_removed_is_idempotent():
    facade = FakeFacade()
    service = SourceTriggerService(
        FakeIngestSourceRepository(), facade,
        source_created=Signal("a"), source_changed=Signal("b"),
        source_deleted=Signal("c"),
    )

    service.on_source_removed(42)
    assert facade.removed == [42]


# =========================================================================== #
# 装配：容器 key、boot / exit 顺序
# =========================================================================== #

class _LifecycleStub:
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log
        # 装配层的接线会读 ``source_runtime.scan_finished`` 并 connect，
        # 所以替身必须也带一个真信号（不是 mock，行为才可断言）。
        self.scan_finished = Signal(f"scan_finished:{name}")
        # 装配层的视图接线会读 ``view_service.assets_viewed``
        self.assets_viewed = Signal(f"assets_viewed:{name}")

    def start(self) -> None:
        self._log.append(f"start:{self._name}")

    def stop(self, wait: bool = True) -> None:  # 缩略图/调度器带 wait 参数
        self._log.append(f"stop:{self._name}")

    def shutdown(self) -> None:
        self._log.append(f"shutdown:{self._name}")

    def trigger(self) -> None:  # ImportService 的兼容面
        self._log.append(f"trigger:{self._name}")

    def stop_worker(self) -> None:  # ImportService 的兼容面
        self._log.append(f"stop_worker:{self._name}")


@pytest.fixture
def assembled_container(clean_hooks):
    """``register_service`` 装好的容器 + 生命周期替身。"""
    from albuswall.command import CommandRegistry
    from albuswall.core import Container

    container = Container()
    container.register(
        "command_registry", CommandRegistry, returns=CommandRegistry
    )
    container.set("ipc_service", object())
    register_service(container)
    return container, clean_hooks


def test_register_service_declares_source_services(assembled_container):
    container, _ = assembled_container
    for key in (
            "source_service", "source_runtime",
            "source_state_service", "source_trigger_service",
            "source_trash_service",
    ):
        assert key in Services.__annotations__
        assert key in container


def test_boot_and_exit_hook_order(assembled_container):
    from albuswall.utils.hooks import run_hooks

    container, hooks = assembled_container
    log: list[str] = []
    for key in (
            "source_runtime", "import_service", "source_state_service",
            "source_trigger_service", "ipc_request_service",
            "thumbnail_service", "trash_cleanup_scheduler",
    ):
        container.set(key, _LifecycleStub(key, log))
    # 回收站没有 start/stop，装配层只 ``connect`` 它的 ``sources_changed``
    container.set("source_trash_service", FakeSourceTrashService())
    container.set("thumbnail_cleaner", _LifecycleStub("thumbnail_cleaner", log))

    run_hooks(hooks["boot"])
    assert [entry for entry in log if entry.startswith("start:")] == [
        "start:source_runtime",
        "start:import_service",
        "start:source_state_service",
        "start:source_trigger_service",
        "start:thumbnail_service",
        "start:trash_cleanup_scheduler",
    ]

    log.clear()
    run_hooks(hooks["loop"], args=(object(),))
    assert log == ["start:ipc_request_service"]

    log.clear()
    run_hooks(hooks["exit"])
    assert log == [
        "stop:trash_cleanup_scheduler",
        "stop:thumbnail_service",
        "stop:source_trigger_service",
        "shutdown:source_state_service",
        "stop_worker:import_service",
        "shutdown:source_runtime",
        "shutdown:ipc_request_service",
    ]


def test_trigger_refresh_signal_reaches_state(assembled_container):
    """``trigger_refresh_signal`` → ``SourceStateService.refresh_source``。"""
    from albuswall.utils.hooks import run_hooks

    container, hooks = assembled_container
    seen: list[int] = []

    class _StateStub(_LifecycleStub):
        def __init__(self) -> None:
            super().__init__("source_state_service", [])

        def refresh_source(self, source_id: int) -> None:
            seen.append(source_id)

    container.set("source_state_service", _StateStub())
    for key in ("source_runtime", "source_trigger_service", "import_service"):
        container.set(key, _LifecycleStub(key, []))
    container.set("ipc_request_service", _LifecycleStub("ipc", []))
    container.set("source_trash_service", FakeSourceTrashService())
    for key in ("thumbnail_service", "trash_cleanup_scheduler",
                "thumbnail_cleaner", "view_service"):
        container.set(key, _LifecycleStub(key, []))

    run_hooks(hooks["boot"])
    container.get("trigger_refresh_signal").emit(None, source_id=7)

    assert seen == [7]


def test_old_source_modules_are_gone():
    """旧的 source 门面与触发器同步模块必须已从 ``old/`` 删除。"""
    old_dir = _SOURCE_PKG.parent / "old"
    assert not (old_dir / "source.py").exists()
    assert not (old_dir / "trigger_sync.py").exists()
    assert not (old_dir / "errors.py").exists()
