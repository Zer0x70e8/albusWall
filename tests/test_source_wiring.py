#
"""装配层验收：扫描完成 → 导入 worker、退出钩子、TrackingState 契约、
回收站分发、``import_config()`` 的兜底语义。

对应「修复任务书」的五项：

* **P0-1** ``SourceRuntime.scan_finished`` → ``ImportService.trigger`` 真的接通；
* **P0-2** 退出钩子用 ``Application`` 真实存在的名字（``on_exit``，**没有**
  ``on_final``），并走一遍真实 ``Application`` 生命周期；
* **P1-3** ``TrackingState.observe`` 的签名 / 时机 / 线程契约（非属主线程
  调 ``mark_dirty`` 抛 ``RuntimeError``）；
* **P1-4** ``_dispatch_trash_change`` 的 ``purged`` 分支与 payload 对齐；

真 infra / 真 DB 的端到端冒烟在本文件末尾（``test_end_to_end_smoke``），
其余用例一律替身 + 不连 DB。
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib
import threading
import time
import types
from typing import Any, Optional

import pytest

from albuswall.core import Container
from albuswall.core.application import Application, CoreApplication
from albuswall.core.main_loop import MainLoop
from albuswall.service import register_service
from albuswall.service.source.state.tracking import TrackingState
from albuswall.utils.signals import Signal

_ROOT = pathlib.Path(
    inspect.getfile(__import__("albuswall.service", fromlist=["x"]))
).parent
_SERVICE_INIT = _ROOT / "__init__.py"
_SOURCE_PKG = _ROOT / "source"
_TRASH_SERVICE = _ROOT / "source" / "trash" / "service.py"
# 会话脏标的钩子已随缩略图域迁到 service/thumbnail/cleaner.py
_CLEANER_MODULE = _ROOT / "thumbnail" / "cleaner.py"


# =========================================================================== #
# 替身
# =========================================================================== #

class Stub:
    """既当服务生命周期替身，又当被接线对象。"""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.trigger_calls: list[dict[str, Any]] = []
        self.scan_finished = Signal(f"scan_finished:{name}")
        # 回收站信号：装配层 ``_wire_source_trash`` 会 ``connect`` 它
        self.sources_changed = Signal(f"sources_changed:{name}")
        # 视图信号：装配层 ``_wire_view_to_thumbnail`` 会 connect 它
        self.assets_viewed = Signal(f"assets_viewed:{name}")
        self.refresh_calls: list[int] = []
        self.upsert_calls: list[int] = []
        self.remove_calls: list[int] = []
        self.reconcile_calls: list[dict[str, Any]] = []

    # 生命周期
    def start(self) -> None:
        self.log.append(f"start:{self.name}")

    def stop(self) -> None:
        self.log.append(f"stop:{self.name}")

    def shutdown(self) -> None:
        self.log.append(f"shutdown:{self.name}")

    def stop_worker(self) -> None:
        self.log.append(f"stop_worker:{self.name}")

    # ImportService 面
    def trigger(self) -> None:
        self.log.append(f"trigger:{self.name}")
        self.trigger_calls.append({})

    # SourceStateService 面
    def refresh_source(self, source_id: int) -> None:
        self.refresh_calls.append(source_id)

    # SourceTriggerService 面（回收站桥接）
    def on_source_upserted(self, source_id: int) -> None:
        self.log.append(f"upsert:{self.name}:{source_id}")
        self.upsert_calls.append(source_id)

    def on_source_removed(self, source_id: int) -> None:
        self.log.append(f"remove:{self.name}:{source_id}")
        self.remove_calls.append(source_id)

    # SourceRuntime 面
    def reconcile(self, **kwargs: Any) -> None:
        self.reconcile_calls.append(kwargs)


class FakeLoop(MainLoop):
    """跑一次 ``on_run`` 就退出的主循环（仿真"UI 起来了又关掉"）。"""

    def __init__(self, on_run: Any = None) -> None:
        self._on_run = on_run
        self.calls: list[str] = []

    def run(self) -> int:
        self.calls.append("run")
        if self._on_run is not None:
            self._on_run()
        return 0

    def quit(self, code=None) -> None:  # pragma: no cover - 不需要
        self.calls.append(f"quit:{code}")


_SERVICE_KEYS = (
    "source_runtime",
    "import_service",
    "source_state_service",
    "source_trigger_service",
    "ipc_request_service",
)


def _build_container_with_stubs() -> tuple[Container, dict[str, Stub], dict[str, list]]:
    """``register_service`` 装好、每个 key 都换成 Stub 的容器。"""
    from albuswall.command import CommandRegistry
    from albuswall.core.registry import hooks_map

    container = Container()
    container.register(
        "command_registry", CommandRegistry, returns=CommandRegistry
    )
    container.set("ipc_service", object())
    register_service(container)

    log: list[str] = []
    stubs = {key: Stub(key, log) for key in _SERVICE_KEYS}
    for key, stub in stubs.items():
        container.set(key, stub)
    # 回收站不在 ``_SERVICE_KEYS`` 里：它没有 start/stop，只有 ``sources_changed``
    # （生命周期归 Reconciler / TaskService 管）。装 stub 后装配层的 on_boot
    # 就不会去构造真服务（真服务要 reconciler + 两个 repo）。
    container.set(
        "source_trash_service", Stub("source_trash_service", log)
    )
    # 缩略图 / 回收站调度 / 清理器 / 视图：boot 钩子会碰它们，装替身以免
    # 每个"只验接线"的用例都去起真实后台线程（线程会活到整个测试会话结束）。
    for key in (
            "thumbnail_service", "thumbnail_cleaner",
            "trash_cleanup_scheduler", "view_service",
    ):
        container.set(key, Stub(key, log))
    return container, stubs, hooks_map


@pytest.fixture
def wired(clean_hooks):
    """容器 + 每个 key 的 Stub；``clean_hooks`` 保证 hook 表用完即还原。"""
    container, stubs, hooks = _build_container_with_stubs()
    return container, stubs, hooks


# =========================================================================== #
# P0-1：scan_finished → ImportService.trigger
# =========================================================================== #

def test_scan_finished_calls_import_trigger_once(wired):
    """``scan_finished.emit(sender, event=...)`` → ``trigger`` 恰好一次。"""
    from albuswall.utils.hooks import run_hooks

    _container, stubs, hooks = wired
    run_hooks(hooks["boot"])

    from albuswall.dto.source.events import SourceScanFinished

    event = SourceScanFinished(
        source_id=7,
        task_seq=3,
        scanned_file_count=5,
        new_file_count=2,
        has_new_content=True,
        inserted_count=2,
    )

    stubs["source_runtime"].scan_finished.emit(object(), event=event)

    assert len(stubs["import_service"].trigger_calls) == 1, (
        "payload 必须被 **_kw 吞掉，且不得影响调用次数"
    )


def test_scan_finished_payload_is_ignored(wired):
    """payload 结构与内容都不参与判断：空 payload / 任意 kwarg 同样触发。"""
    from albuswall.utils.hooks import run_hooks

    _container, stubs, hooks = wired
    run_hooks(hooks["boot"])

    signal = stubs["source_runtime"].scan_finished
    signal.emit(object())
    signal.emit(object(), event=None, whatever={"x": 1})

    assert len(stubs["import_service"].trigger_calls) == 2


def test_scan_finished_wiring_is_real_not_todo():
    """装配层必须真的 connect，而不是只留 TODO 注释。"""
    source = _SERVICE_INIT.read_text(encoding="utf-8")

    assert 'scan_finished.connect(' in source
    assert '_wire_scan_to_import' in source
    # ImportService 已迁到 service/importer/，"临时直连"那套说辞必须消失
    assert "临时直连" not in source
    assert "service.old" not in source
    assert '"import_service"' in source
    assert "import_service: ImportService" in source


def test_import_service_is_registered_and_constructible():
    """``Services`` 声明了 import_service，且 _ARG_MAP 按 old 签名取参数。"""
    from albuswall.service import _ARG_MAP, Services

    assert Services.__annotations__["import_service"].__name__ == "ImportService"
    factory = _ARG_MAP["import_service"]

    container = Container()
    container.set("import_repo", "REPO")
    container.set("task_service", "TASK")
    container.set("ingest_source_repo", "SOURCE")
    built = factory(lambda *args: ("built", args), container)
    assert built == ("built", ("REPO", "TASK", "SOURCE"))


# =========================================================================== #
# P0-2：退出钩子名 & 真实 Application 生命周期
# =========================================================================== #

def test_application_exposes_on_exit_and_no_on_final():
    """``Application`` 只有 on_boot/on_init/on_loop/on_exit（无 on_final）。"""
    for name in ("on_boot", "on_init", "on_loop", "on_exit"):
        assert callable(getattr(Application, name)), name
    assert not hasattr(Application, "on_final")
    assert not hasattr(CoreApplication, "on_final")


def test_assembly_registers_only_exit_hooks():
    """装配层不得出现 ``on_final``；三项拆解都挂在 ``on_exit`` 上。"""
    tree = ast.parse(_SERVICE_INIT.read_text(encoding="utf-8"))
    called: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Application"
        ):
            called.append(node.func.attr)

    assert "on_final" not in called
    assert called.count("on_exit") >= 4  # trigger/state/import/runtime(+ipc)
    assert "on_boot" in called


def test_real_application_lifecycle_stops_every_source_service(clean_hooks):
    """走完整 ``boot → loop → exit``（不手调 shutdown），三项拆解各一次。"""
    log: list[str] = []
    loop = FakeLoop()
    app = CoreApplication(main_loop=loop)
    app.container.set("config", types.SimpleNamespace(tree=types.SimpleNamespace(main={})))
    app.resolve_slot("config")
    app.resolve_slot("plugins")

    register_service(app.container)

    from albuswall.command import CommandRegistry

    app.container.register(
        "command_registry", CommandRegistry, returns=CommandRegistry
    )
    app.container.set("ipc_service", object())
    stubs = {key: Stub(key, log) for key in _SERVICE_KEYS}
    for key, stub in stubs.items():
        app.container.set(key, stub)
    # 回收站无 start/stop：给它 stub 只是为了装配层能 ``connect`` 到信号
    app.container.set(
        "source_trash_service", Stub("source_trash_service", log)
    )

    app.exec()

    assert log == [
        "start:source_runtime",
        "start:import_service",
        "start:source_state_service",
        "start:source_trigger_service",
        "start:ipc_request_service",
        "stop:source_trigger_service",
        "shutdown:source_state_service",
        "stop_worker:import_service",
        "shutdown:source_runtime",
        "shutdown:ipc_request_service",
    ]
    # 没有任何一项被调用两次（钩子没被重复登记）
    for key in _SERVICE_KEYS:
        assert log.count(f"start:{key}") == 1


# =========================================================================== #
# P1-3：TrackingState 契约
# =========================================================================== #

def test_observe_runs_on_owner_thread_and_once():
    from albuswall.dto.source.query import IngestSource

    state = TrackingState()
    seen: list[tuple[TrackingState, threading.Thread]] = []
    state.observe(lambda node: seen.append((node, threading.current_thread())))

    state.upsert_source(1, IngestSource(id=1, source_path="/tmp/x"))

    assert len(seen) == 1
    node, thread = seen[0]
    assert node is state
    assert thread is threading.current_thread()
    assert thread.ident == state.owner_thread_id


def test_mark_dirty_off_owner_thread_raises():
    """docstring 承诺：非属主线程 → ``RuntimeError``（不是 warning + 静默）。"""
    from albuswall.dto.source.query import IngestSource

    state = TrackingState()
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            state.mark_dirty("sources")
        except BaseException as exc:  # noqa: BLE001 - 收集起来在主线程断言
            errors.append(exc)

    thread = threading.Thread(target=_worker, name="albuswall-test-worker")
    thread.start()
    thread.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "owner thread" in str(errors[0])

    # 越界写也不得改到状态：守卫在写入**之前**生效
    def _write() -> None:
        try:
            state.upsert_source(2, IngestSource(id=2, source_path="/tmp/y"))
        except RuntimeError:
            pass

    writer = threading.Thread(target=_write)
    writer.start()
    writer.join(timeout=5)
    assert len(state) == 0


def test_observe_signature_matches_documented_contract():
    """``observe(cb)``：cb 只收一个位置参数（旧的 ``cb(node, key)`` 已废弃）。"""
    signature = inspect.signature(TrackingState.observe)
    assert list(signature.parameters) == ["self", "callback"]

    def callback(state: TrackingState) -> None:  # 新契约
        return None

    params = list(inspect.signature(callback).parameters.values())
    assert [p.name for p in params] == ["state"]
    assert all(
        p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in params
    )

    # 文档里必须写明与旧契约的差异（回调签名 / 返回值 / 线程 / 时机）
    class_doc = TrackingState.__doc__ or ""
    module_doc = inspect.getmodule(TrackingState).__doc__ or ""
    doc = class_doc + "\n" + module_doc

    assert "cb(state" in doc, "必须写明新回调签名"
    assert "cb(node, key" in doc, "必须记下旧签名的差异"
    assert "unobserve" in doc, "必须写明退订方式（旧的是返回闭包）"
    assert "RuntimeError" in doc, "必须写明越界线程的行为"
    assert "同步" in doc, "必须写明触发时机"


def test_old_style_two_arg_callback_is_logged_not_silent(caplog):
    """旧两参回调不会"静默失效"：异常被记成 ERROR 日志。"""
    from albuswall.dto.source.query import IngestSource

    state = TrackingState()
    called: list[str] = []

    def legacy(node, key):  # 旧的 ObservableNamespace 契约 → 必然 TypeError
        called.append(key)

    state.observe(legacy)

    with caplog.at_level(logging.ERROR):
        state.upsert_source(1, IngestSource(id=1, source_path="/tmp/z"))

    assert called == [], "旧两参回调不该成功执行（它会 TypeError）"
    assert any(
        record.levelno >= logging.ERROR
        and "tracking observer raised" in record.getMessage()
        for record in caplog.records
    ), "回调异常必须留 ERROR 日志，不能静默吞掉"


def test_unobserve_stops_callbacks():
    from albuswall.dto.source.query import IngestSource

    state = TrackingState()
    hits: list[int] = []
    callback = hits.append
    state.observe(lambda node: callback(1))
    state.unobserve(state._observers[0])  # noqa: SLF001 - 直接拿引用退订
    state.upsert_source(1, IngestSource(id=1, source_path="/tmp/u"))

    assert hits == []


# =========================================================================== #
# P1-4：回收站分发（purged 分支 + payload 对齐）
# =========================================================================== #

def _dispatch(container: Container):
    from albuswall.service import _dispatch_trash_change

    return _dispatch_trash_change(container)


def test_purged_event_never_touches_state(wired):
    _container, stubs, _hooks = wired
    container = _container

    _dispatch(container)(None, source_id=7, kind="purged")

    assert stubs["source_state_service"].refresh_calls == []
    assert stubs["source_trigger_service"].reconcile_calls == []
    # 触发器侧也不应收到 remove
    assert stubs["source_trigger_service"].log == []


@pytest.mark.parametrize("kind", ["soft_delete", "restore"])
def test_visible_kinds_refresh_state_exactly_once(wired, kind):
    _container, stubs, _hooks = wired

    _dispatch(_container)(None, source_id=11, kind=kind)

    assert stubs["source_state_service"].refresh_calls == [11]


def test_soft_delete_removes_trigger_and_restore_upserts(clean_hooks):
    """分发层对触发器桥接层的两种动作（remove / upsert）。"""
    from albuswall.service import _dispatch_trash_change

    calls: list[tuple[str, int]] = []

    class _Trigger:
        def on_source_upserted(self, source_id: int) -> None:
            calls.append(("upsert", source_id))

        def on_source_removed(self, source_id: int) -> None:
            calls.append(("remove", source_id))

    class _State:
        def refresh_source(self, source_id: int) -> None:  # pragma: no cover
            pass

    lookup = types.SimpleNamespace(
        get=lambda name: {"source_state_service": _State(),
                          "source_trigger_service": _Trigger()}[name]
    )
    dispatch = _dispatch_trash_change(lookup)

    dispatch(None, source_id=1, kind="soft_delete")
    dispatch(None, source_id=2, kind="restore")

    assert calls == [("remove", 1), ("upsert", 2)]


def test_dispatch_accepts_keyword_payload_only():
    """payload 必须是关键字：位置参数调用直接 TypeError（签名即契约）。"""
    signature = inspect.signature(
        __import__(
            "albuswall.service", fromlist=["x"]
        )._dispatch_trash_change(object())
    )
    kinds = {
        name: param.kind
        for name, param in signature.parameters.items()
    }
    assert kinds["source_id"] is inspect.Parameter.KEYWORD_ONLY
    assert kinds["kind"] is inspect.Parameter.KEYWORD_ONLY


def test_placeholder_signal_rejects_positional_payload():
    """占位信号走 ``Signal``：位置参数 payload 会被拒（只能 kwargs）。"""
    signal = Signal("SourceTrashChanged")

    with pytest.raises(TypeError):
        signal.emit(None, 7, "soft_delete")  # type: ignore[misc]


def test_source_trash_changed_emits_keyword_payload():
    """真实的 ``SourceTrashService.sources_changed`` 也是 kwargs —— payload 对齐。"""
    tree = ast.parse(_TRASH_SERVICE.read_text(encoding="utf-8"))
    emits: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "sources_changed"
        ):
            emits.append(node)

    assert emits, "没找到 sources_changed.emit 调用"
    for call in emits:
        assert not call.args[1:], "payload 不得用位置参数"
        names = {kw.arg for kw in call.keywords}
        assert {"source_id", "kind"} <= names


def test_purged_branch_returns_before_anything_else():
    """源码契约：purged 分支注释 + 第一行 return。"""
    source = _SERVICE_INIT.read_text(encoding="utf-8")
    lines = source.splitlines()

    idx = next(
        i for i, line in enumerate(lines)
        if "# purged 在 reconciler 工作线程 emit，禁止碰 state。" in line
    )
    assert 'if kind == "purged":' in lines[idx + 1]
    assert lines[idx + 2].strip() == "return", lines[idx + 2]


# ---- import_config() 的两条路径：正常取值 / 回退记 WARNING --------------- #

_IMPORT_INI = """\
[import]
batch_size = 7
process_timeout = 12.5
initial_backoff = 0.25
max_backoff = 3.0
"""


def _raising_config(exc: BaseException) -> type:
    """造一个"构造即抛指定异常"的 ``ImportConfig`` 替身。"""

    def _init(self) -> None:
        raise exc

    return type("ImportConfig", (), {"__init__": _init})


def _install_import_config_class():
    """把真正的 ``ImportConfig`` 幂等登记回全局表。

    ``tests/test_configue/conftest.py`` 的 autouse fixture 会清空
    ``registry._CLASSES``，跑完整套件时这里可能已经空了。
    """
    from albuswall.configue import configclass
    from albuswall.service.importer import ImportConfig
    from albuswall.service.source.trash.config import SourceTrashSettings

    configclass("import", "main")(ImportConfig)
    # 回收站的 [source_trash] 段同理（``Configue`` 缺席时会退回声明默认值，
    # 但这里补上更接近生产：配置真的被读，而不是走兜底 + WARNING）。
    configclass("source_trash", "main")(SourceTrashSettings)
    return ImportConfig


def test_import_config_uses_user_values_without_fallback(tmp_path, caplog):
    """配置系统可用且文件里写了值 → 返回**用户值**，且不回退。"""
    import logging

    from albuswall.configue import Configue
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.service import importer as old_import

    _install_import_config_class()
    base = tmp_path / "cfg"
    base.mkdir()
    (base / "config.ini").write_text(_IMPORT_INI, encoding="utf-8")

    previous = Configue._instance
    config = setup_config(
        Container(), base_dir=base, file_name="config.ini", portable=True,
    )
    try:
        with caplog.at_level(logging.WARNING):
            conf = old_import.import_config()

        assert dict(conf) == {
            "batch_size": 7,
            "process_timeout": 12.5,
            "initial_backoff": 0.25,
            "max_backoff": 3.0,
        }, "必须拿到配置文件里的值，而不是声明默认值"
        # 非默认值本身即可证明"没走回退分支"；日志层面再确认一次
        assert not [
            r for r in caplog.records
            if "falling back to declared defaults" in r.getMessage()
        ], "正常情况下不该出现回退日志"
    finally:
        config.close()
        Configue._instance = previous


def test_import_config_reads_values_registered_after_load(tmp_path):
    """生产顺序（先 ``load()`` 再 import service 包）也拿到**强类型**用户值。

    真实启动里 ``setup_config`` 在 phase 1、``register_service`` 在 phase 7，
    因此 ``ImportConfig`` 是"晚注册"。晚注册路径若不登记类型，读到的会是
    文件里的原始字符串（``"7"``），这条用例把该回归钉住。
    """
    from albuswall.configue import Configue
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.service import importer as old_import

    base = tmp_path / "cfg"
    base.mkdir()
    (base / "config.ini").write_text(_IMPORT_INI, encoding="utf-8")

    previous = Configue._instance
    config = setup_config(
        Container(), base_dir=base, file_name="config.ini", portable=True,
    )
    try:
        _install_import_config_class()  # 等价于"load 之后才 import"
        conf = old_import.import_config()

        assert conf["batch_size"] == 7 and isinstance(conf["batch_size"], int)
        assert isinstance(conf["process_timeout"], float)
        assert isinstance(conf["initial_backoff"], float)
    finally:
        config.close()
        Configue._instance = previous


def test_import_config_falls_back_with_warning_when_config_absent(
        monkeypatch, caplog,
):
    """配置系统缺席 → 回退声明默认值，**并且记 WARNING**（不是静默）。"""
    import logging

    from albuswall.configue.errors import ConfigMissingError
    from albuswall.service.importer import config as import_cfg

    monkeypatch.setattr(
        import_cfg,
        "ImportConfig",
        _raising_config(ConfigMissingError("Configue not initialized")),
    )

    with caplog.at_level(logging.WARNING):
        conf = import_cfg.import_config()

    assert conf == {
        "batch_size": 500,
        "process_timeout": 600.0,
        "initial_backoff": 1.0,
        "max_backoff": 60.0,
    }
    warnings_ = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING
        and "falling back to declared defaults" in r.getMessage()
    ]
    assert warnings_, "回退必须是可观测的 WARNING，不能静默"
    assert "ConfigMissingError" in warnings_[0], "告警里要带异常类型，便于定位"


def test_import_config_falls_back_on_invalid_value(monkeypatch, caplog):
    """配置值非法（``_convert`` 抛裸 ``ValueError``）同样回退 + WARNING。

    这条是"兜 ``ValueError``"的理由：它不是 ``ConfigError``，漏了就打死
    import worker 线程。
    """
    import logging

    from albuswall.service.importer import config as import_cfg

    monkeypatch.setattr(
        import_cfg,
        "ImportConfig",
        _raising_config(ValueError("failed to convert 'abc' to <class 'int'>")),
    )

    with caplog.at_level(logging.WARNING):
        conf = import_cfg.import_config()

    assert conf["batch_size"] == 500
    assert any(
        "ValueError" in r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.parametrize(
    "exc", [RuntimeError("some programming error"), TypeError("wrong type")],
)
def test_import_config_does_not_swallow_programming_errors(monkeypatch, exc):
    """兜底范围收窄的证据：非"配置不可用"的异常必须原样抛出。"""
    from albuswall.service.importer import config as import_cfg

    monkeypatch.setattr(import_cfg, "ImportConfig", _raising_config(exc))

    with pytest.raises(type(exc)):
        import_cfg.import_config()


def test_registry_raises_specific_exception_for_unregistered_class():
    """registry 的裸 ``RuntimeError`` 已收敛成 ``ConfigClassNotRegistered``。"""
    from albuswall.configue import ConfigClassNotRegistered, configclass
    from albuswall.configue.errors import ConfigError
    from albuswall.configue.registry import _CLASSES

    # 双重继承：历史 (RuntimeError) 与"配置系统一棵树" (ConfigError) 都成立
    assert issubclass(ConfigClassNotRegistered, RuntimeError)
    assert issubclass(ConfigClassNotRegistered, ConfigError)

    @configclass("probe", "main")
    class Probe:
        value: int = 1

    # 同名 section 的另一份声明现在是共享（声明等价即可），不再把 Probe 顶掉；
    # 真正触发该异常的是"类不在登记表里"——这里直接把 Probe 从表里摘掉模拟。
    @configclass("probe", "main")
    class Shadow:
        value: int = 1

    assert Probe in _CLASSES and Shadow in _CLASSES   # 共享，不是覆盖

    _CLASSES.pop(Probe)
    with pytest.raises(ConfigClassNotRegistered) as info:
        Probe().value
    assert "not registered in _CLASSES" in str(info.value)
    # 兼容既有捕获方式：RuntimeError / ConfigError 都能抓到
    with pytest.raises(RuntimeError):
        Probe().value
    with pytest.raises(ConfigError):
        Probe().value


# ---- source_trash 生命周期钩子（on_boot / on_exit）----------------------- #

class _RecordingMarker:
    """``SessionMarker`` 替身：记录 mark_running / mark_clean 的调用。"""

    instances: list["_RecordingMarker"] = []

    def __init__(self, path: Any) -> None:
        self.path = path
        self.calls: list[str] = []
        _RecordingMarker.instances.append(self)

    def is_clean_shutdown(self) -> bool:
        self.calls.append("is_clean_shutdown")
        return True

    def mark_running(self) -> None:
        self.calls.append("mark_running")

    def mark_clean(self) -> None:
        self.calls.append("mark_clean")


def _load_legacy_lifecycle(
        *,
        replace: Optional[tuple[str, str]] = None,
        marker_exists: bool = True,
):
    """把 ``install_dirty_recovery`` 的函数体取出来直接执行。

    为什么这样做：真函数在 import 期就要读配置（``load_session_config()``，
    需要 ``Configue``）并构造 ``SessionMarker``，而本组用例要验的是**这个
    函数自己的钩子接线**（boot → mark_running，exit → mark_clean）。因此把
    函数源码从文件里解析出来、只注入它真正用到的名字再执行：测到的仍是仓库
    里那份真实代码，不需要伪造一整套假配置。

    历史：本函数原叫 ``install_source_trash_lifecycle``，住在
    ``service/old/source_trash/bootstrap.py``；随缩略图域迁到
    ``service/thumbnail/cleaner.py`` 并改名（它管的是"脏退出后清缩略图孤儿"，
    跟源回收站没关系）。
    """
    source = _CLEANER_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "install_dirty_recovery"
    )
    segment = ast.get_source_segment(source, fn)
    assert segment, "取不到 install_dirty_recovery 的源码"
    if replace is not None:
        before, after = replace
        assert before in segment, f"源码里找不到 {before!r}"
        segment = segment.replace(before, after)

    _RecordingMarker.instances.clear()
    # 三态语义：标记文件不存在 = 首次运行 → 不扫缓存。这里按需造一个真实
    # 路径，让"存在"与"不存在"两条分支都能被测到。
    import tempfile

    marker_dir = pathlib.Path(tempfile.mkdtemp(prefix="albuswall-session-"))
    marker_path = marker_dir / ".session"
    if marker_exists:
        marker_path.write_text("clean\n", encoding="utf-8")
    namespace: dict[str, Any] = {
        "load_session_config": lambda: types.SimpleNamespace(
            marker_path=marker_path,
            auto_cleanup_on_dirty=True,
        ),
        "SessionMarker": _RecordingMarker,
        "_logger": logging.getLogger("thumbnail-cleaner"),
    }
    exec(compile(segment, str(_CLEANER_MODULE), "exec"), namespace)
    return namespace["install_dirty_recovery"]


class _Loop(MainLoop):
    def run(self) -> int:
        return 0

    def quit(self, code=None) -> None:  # pragma: no cover
        pass


def _real_app() -> CoreApplication:
    app = CoreApplication(main_loop=_Loop())
    app.container.set(
        "config", types.SimpleNamespace(tree=types.SimpleNamespace(main={}))
    )
    app.container.set("thumbnail_cleaner", Stub("thumbnail_cleaner", []))
    app.resolve_slot("config")
    app.resolve_slot("plugins")
    return app


def test_source_trash_lifecycle_marks_running_and_clean_once(clean_hooks):
    """真实 ``Application`` 生命周期：boot → mark_running，exit → mark_clean。"""
    install = _load_legacy_lifecycle()
    app = _real_app()

    # 不传 app：走默认路径（Application 门面类的 classmethod）
    install(app.container)
    app.exec()  # 不手调钩子：由 Application 自己跑 boot → loop → exit

    markers = _RecordingMarker.instances
    assert len(markers) == 1
    calls = markers[0].calls
    assert calls.count("mark_running") == 1, calls
    assert calls.count("mark_clean") == 1, calls
    # boot 阶段先读干净标记，再置脏（顺序反了会永远读到自己的脏标）
    assert calls.index("is_clean_shutdown") < calls.index("mark_running")


def test_dirty_recovery_skips_cleanup_when_no_marker(clean_hooks):
    """标记文件不存在 = 首次运行，不扫缓存（三态语义，见 cleaner.py）。"""
    install = _load_legacy_lifecycle(marker_exists=False)
    app = _real_app()
    cleaned: list[str] = []
    app.container.set(
        "thumbnail_cleaner",
        types.SimpleNamespace(
            scan_and_purge=lambda: cleaned.append("scan"),
        ),
    )

    install(app.container)
    app.exec()

    assert cleaned == [], "没有会话标记时不该去扫缩略图缓存"
    marker = _RecordingMarker.instances[0]
    assert marker.calls.count("mark_running") == 1


def test_source_trash_lifecycle_fails_loudly_on_wrong_hook_name(clean_hooks):
    """钩子名写错（``on_final``）→ 安装当场 ``AttributeError``，不是静默跳过。

    顺带钉住"如果把这个异常吞掉会怎样"：``on_boot`` 在报错**之前**已经装上，
    而 ``on_exit`` 没装上 —— 于是 ``mark_running`` 会跑、``mark_clean`` 永远
    不跑，session marker 永久留脏。这正是"静默跳过"的代价，也是这里必须
    让它当场炸的原因。
    """
    install = _load_legacy_lifecycle(
        replace=(
            "Application.on_exit(_on_exit)",
            "Application.on_final(_on_exit)",
        )
    )
    app = _real_app()

    with pytest.raises(AttributeError) as info:
        install(app.container)
    assert "on_final" in str(info.value)

    # 报错前 on_boot 已经装上 → mark_running 会跑；on_exit 没装 → mark_clean 不跑
    app.exec()
    calls = _RecordingMarker.instances[0].calls
    assert calls.count("mark_running") == 1, calls
    assert calls.count("mark_clean") == 0, calls


def test_no_code_calls_the_nonexistent_on_final():
    """``src/`` 里不得再有 ``.on_final(...)`` 调用（注释/文档提到它不算）。"""
    offenders: list[str] = []
    for path in pathlib.Path(
        inspect.getfile(__import__("albuswall", fromlist=["x"]))
    ).parent.rglob("*.py"):
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1,
        ):
            code = line.split("#", 1)[0]
            if ".on_final(" in code:
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], offenders
    assert not hasattr(Application, "on_final")


# =========================================================================== #
# 端到端冒烟：真 infra + 临时 DB + 临时配置
# =========================================================================== #

def _make_real_app(tmp_path: pathlib.Path, on_run: Any) -> CoreApplication:
    """真 infra + 临时 DB / 临时配置，但**不注册 IPC**。

    ``register_ipc`` 会把 endpoint 绑到 ``/run/user/<uid>/albuswall/ipc.sock``，
    在沙箱 / CI 里那个目录可能只读，绑定失败与被测逻辑无关。本冒烟只需要
    db / task / trigger / repository 四类，因此单独注册它们，并把
    ``ipc_request_service`` 换成替身（``on_loop`` 钩子只对它调 ``start()``）。
    """
    from albuswall.command import registry_command_registry
    from albuswall.configue import configclass
    from albuswall.configue.bootstrap import setup_config
    from albuswall.infrastructure import (
        register_database,
        register_reconciler,
        register_task_service,
        register_trigger_service,
    )
    from albuswall.repositories import registry_repository

    # ``tests/test_configue/conftest.py`` 的 autouse fixture 会清空
    # ``registry._CLASSES``（那套测试自带隔离），于是跑完整套件时本进程里
    # 导入期登记的 ``@configclass`` 已经不在表里了。这里按原声明幂等重建
    # 一次（同一个类对象 → 原地刷新 slot 与默认值），否则 ``db`` 工厂读
    # ``Config().must_exist`` 会抛 "not registered in _CLASSES"。
    from albuswall.infrastructure.database import bootstrap as db_bootstrap
    from albuswall.infrastructure.task import bootstrap as task_bootstrap

    from albuswall.service.importer import ImportConfig
    from albuswall.service.source.trash.config import SourceTrashSettings

    configclass("db", "main")(db_bootstrap.Config)
    configclass("task", "main")(task_bootstrap.Config)
    configclass("import", "main")(ImportConfig)
    # 回收站的 [source_trash] 段同理（``Configue`` 缺席时会退回声明默认值，
    # 但这里补上更接近生产：配置真的被读，而不是走兜底 + WARNING）。
    configclass("source_trash", "main")(SourceTrashSettings)

    loop = FakeLoop(on_run)
    app = CoreApplication(main_loop=loop)
    setup_config(
        app.container,
        base_dir=tmp_path / "cfg",
        file_name="config.ini",
        ignore_missing=True,
        portable=True,
    )
    app.resolve_slot("config")
    app.resolve_slot("plugins")

    registry_command_registry(app.container)
    register_database(app.container)
    register_task_service(app.container)
    register_trigger_service(app.container)
    # 回收站要 ``reconciler``：本服务在 on_boot 构造时就向它注册对账条目
    # （``on_startup=True``），缺 key 会 MissingDependencyError。
    register_reconciler(app.container)
    registry_repository(app.container)
    register_service(app.container)
    app.container.set("ipc_request_service", Stub("ipc_request_service", []))
    return app


def _named_threads(prefixes: tuple[str, ...]) -> list[str]:
    return [
        t.name for t in threading.enumerate()
        if t.is_alive() and t.name.startswith(prefixes)
    ]


def test_end_to_end_smoke(tmp_path, clean_hooks):
    """create → 扫描 → trigger；disable / enable / delete；退出后无残留线程。"""
    from albuswall.dto.source.command import IngestSourceCreate

    evidence: dict[str, Any] = {}
    pics = tmp_path / "pics"
    pics.mkdir()

    def _on_run() -> None:
        container = app.container
        cmd = container.get("source_service")
        state = container.get("source_state_service")
        runtime = container.get("source_runtime")
        import_service = container.get("import_service")

        # 计数器包住真实的 ImportService.trigger（不改它的行为）
        calls: list[int] = []
        original_trigger = import_service.trigger
        import_service.trigger = lambda: (calls.append(1), original_trigger())[1]

        source_id = cmd.create(IngestSourceCreate(
            title="smoke", source_path=str(pics), file_types=["png"],
        ))
        evidence["source_id"] = source_id
        evidence["tracked_after_create"] = source_id in state.tracked_sources
        evidence["tasks_after_create"] = len(runtime.tasks)

        # 等一轮：TaskService 跑完 executor → scan_finished → import worker
        deadline = time.monotonic() + 10.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        evidence["trigger_calls"] = len(calls)
        evidence["import_thread"] = "albuswall-import-worker" in [
            t.name for t in threading.enumerate()
        ]


        cmd.disable(source_id)
        evidence["tracked_after_disable"] = source_id in state.tracked_sources
        evidence["tasks_after_disable"] = len(runtime.tasks)

        cmd.enable(source_id)
        evidence["tracked_after_enable"] = source_id in state.tracked_sources
        evidence["tasks_after_enable"] = len(runtime.tasks)

        cmd.delete(source_id)
        evidence["tracked_after_delete"] = source_id in state.tracked_sources
        evidence["tasks_after_delete"] = len(runtime.tasks)

    app = _make_real_app(tmp_path, _on_run)
    app.exec()

    # ---- 断言：链路 ----
    assert evidence["tracked_after_create"] is True
    assert evidence["tasks_after_create"] == 1
    assert evidence["trigger_calls"] >= 1, "scan_finished 没把导入 worker 叫起来"
    assert evidence["import_thread"] is True, evidence

    # ---- 断言：disable / enable / delete ----
    assert evidence["tracked_after_disable"] is False
    assert evidence["tasks_after_disable"] == 0
    assert evidence["tracked_after_enable"] is True
    assert evidence["tasks_after_enable"] == 1
    assert evidence["tracked_after_delete"] is False
    assert evidence["tasks_after_delete"] == 0

    # ---- 断言：退出后无残留线程 ----
    stray = _named_threads(
        ("albuswall-", "TaskScheduler-", "TaskResourceMonitor")
    )
    assert stray == [], f"退出后仍有残留线程: {stray}"


def test_trash_round_trip_end_to_end(tmp_path, clean_hooks):
    """真栈跑一遍回收站：trash → 自动 purge → restore → 带资产的源。

    与 ``test_source_trash_service.py`` 的分工：那边用替身测**契约**，这里用
    真 sqlite / 真 Reconciler / 真 TaskService 测**装配与事务**——
    尤其是「软删 → debounce → Reconciler → TaskService → purge」这条跨线程
    链路，出问题时只在真栈上现形。
    """
    from uuid import uuid4

    from albuswall.dto.source.command import IngestSourceCreate
    from albuswall.service.command.dispatch_errors import (
        translate_service_error,
    )
    from albuswall.service.command.dispatcher import Dispatcher

    evidence: dict[str, Any] = {}
    pics = tmp_path / "pics"
    pics.mkdir()

    def _on_run() -> None:
        from albuswall.service.command.handlers import setup as setup_handlers

        container = app.container
        cmd = container.get("source_service")
        trash = container.get("source_trash_service")
        db = container.get("db")
        registry = container.get("command_registry")
        # 本用例要经命令层验证「参数 → 服务 → 数据库」整条路，
        # 而 ``_make_real_app`` 只登记了 registry（插件负责装配）。
        setup_handlers(registry, container)
        dispatch = Dispatcher(
            registry, exception_translator=translate_service_error,
        ).dispatch

        # ---- 1) 空源：trash → 立刻不可见 → 自动 purge ----
        sid = cmd.create(IngestSourceCreate(
            title="trash-me", source_path=str(pics), disabled=True,
        ))
        evidence["sid"] = sid
        evidence["trash"] = dispatch("source.trash", [sid], {})
        evidence["visible_after_trash"] = sid in [
            r["id"] for r in dispatch("source.list", [], {})
        ]
        # 幂等：再删一次不算错，返回 False
        evidence["trash_again"] = dispatch("source.trash", [sid], {})
        # 虚拟根：经命令层后必须是映射过的 code（USAGE_ERROR），
        # 而不是兜底的 command_error —— 后者会让调用方以为这是软件错误。
        try:
            dispatch("source.trash", [0], {})
        except Exception as exc:  # noqa: BLE001
            evidence["virtual_root"] = getattr(exc, "code", None)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not db.connect().execute(
                "SELECT 1 FROM ingest_source WHERE id = ?", (sid,)
            ).fetchone():
                break
            time.sleep(0.05)
        evidence["purged"] = not db.connect().execute(
            "SELECT 1 FROM ingest_source WHERE id = ?", (sid,)
        ).fetchone()

        # ---- 2) restore：软删之后、purge 之前救回来 ----
        sid2 = cmd.create(IngestSourceCreate(
            title="restore-me", source_path=str(pics), disabled=True,
        ))
        trash.delete_source(sid2)
        evidence["restore"] = trash.restore_source(sid2)
        evidence["visible_after_restore"] = sid2 in [
            r["id"] for r in dispatch("source.list", [], {})
        ]

        # ---- 3) 带资产的源：source.delete 拒绝，trash 放行，purge 连资产一起删 ----
        sid3 = cmd.create(IngestSourceCreate(
            title="has-assets", source_path=str(pics), disabled=True,
        ))
        evidence["sid3"] = sid3
        conn = db.connect()
        for i in range(3):
            conn.execute(
                "INSERT INTO assets (uuid, file_path, source_id, original_name,"
                " mime_type, file_hash) VALUES (?,?,?,?,?,?)",
                (str(uuid4()), f"p{i}.png", sid3, f"p{i}.png",
                 "image/png", f"hash{i}"),
            )
        conn.execute(
            "INSERT INTO asset_candidate_cache (uuid, path, source_id)"
            " VALUES (?,?,?)", (str(uuid4()), "p0.png", sid3),
        )
        conn.commit()

        try:
            dispatch("source.delete", [sid3], {})
            evidence["delete_with_assets"] = None       # 不该走到这
        except Exception as exc:  # noqa: BLE001
            evidence["delete_with_assets"] = getattr(exc, "code", None)
        evidence["visible_after_refused_delete"] = sid3 in [
            r["id"] for r in dispatch("source.list", [], {})
        ]

        evidence["trash_with_assets"] = dispatch("source.trash", [sid3], {})
        report = trash.purge_source_now(sid3)
        evidence["purged_assets"] = len(report.asset_ids)
        evidence["left_assets"] = conn.execute(
            "SELECT COUNT(*) AS c FROM assets WHERE source_id = ?", (sid3,)
        ).fetchone()["c"]
        evidence["left_candidates"] = conn.execute(
            "SELECT COUNT(*) AS c FROM asset_candidate_cache"
            " WHERE source_id = ?", (sid3,),
        ).fetchone()["c"]

    app = _make_real_app(tmp_path, _on_run)
    app.exec()

    sid = evidence["sid"]
    assert evidence["trash"] == {"id": sid, "trashed": True}
    assert evidence["visible_after_trash"] is False, "软删后必须立刻不可见"
    assert evidence["trash_again"] == {"id": sid, "trashed": False}, "幂等"
    assert evidence["virtual_root"] == "source_virtual_root"
    assert evidence["purged"] is True, "debounce 之后应由 Reconciler 自动 purge"

    assert evidence["restore"] is True
    assert evidence["visible_after_restore"] is True

    # 带资产的源：永久删除被拒绝（保护），回收站放行（正常路径）
    assert evidence["delete_with_assets"] == "source_delete_failed"
    assert evidence["visible_after_refused_delete"] is True
    assert evidence["trash_with_assets"] == {
        "id": evidence["sid3"], "trashed": True}
    assert evidence["purged_assets"] == 3
    assert evidence["left_assets"] == 0, "assets 行必须随源一起删掉"
    assert evidence["left_candidates"] == 0, "候选缓存靠外键 CASCADE"

    stray = _named_threads(
        ("albuswall-", "TaskScheduler-", "TaskResourceMonitor")
    )
    assert stray == [], f"退出后仍有残留线程: {stray}"
