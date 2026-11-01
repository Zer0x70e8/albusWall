#
"""真启动链路：``main.py`` 的装配 → 命令 → 服务写入口回主线程。

这是唯一同时覆盖下面四件事的用例（其它用例只覆盖其中一两件）：

1. ``main.py`` phase 4 的 ``container.set("main_loop", loop)`` —— 少了这一行
   ``MainLoopService`` 会退化成同步执行，命令仍在 ``ipc-conn`` 线程里写库 +
   发信号，状态层的属主断言就会炸；而且**炸了也看不见**：``Signal.emit``
   只记日志、不向调用方抛。所以这一行必须被钉住；
2. ``setup_handlers`` + ``freeze()`` 之后命令真的可执行；
3. handler 留在 ``ipc-conn`` 线程，服务把工作搬到主循环线程；
4. 服务层异常穿过命令层后带上正确的 ``code``。

用真 infra（真 sqlite / 真 TaskService）+ 临时目录；不绑 IPC 端点。
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from albuswall.command import Command, CommandRegistry
from albuswall.core import Application
from albuswall.core.application import CoreApplication
from albuswall.core.main_loop import HeadlessMainLoop

#: ``TrackingState`` 越界写的报错特征串（日志里出现它 = hop 没生效）。
STATE_OWNER_ERROR = "must only be mutated on its owner thread"


# ==================== 装配（对齐 main.py 的 phase 顺序） ====================

class _IpcStub:
    """``ipc_service`` 替身：只保留 handler 挂载点，不绑真实端点。"""

    def __init__(self) -> None:
        self.handler = None

    def set_handler(self, fn) -> None:
        self.handler = fn

    def clear_handler(self) -> None:
        self.handler = None


def _build_app(tmp_path: Path) -> tuple[CoreApplication, HeadlessMainLoop]:
    from albuswall.configue import configclass
    from albuswall.configue.bootstrap import setup_config
    from albuswall.infrastructure import (
        register_database,
        register_reconciler,
        register_task_service,
        register_trigger_service,
    )
    from albuswall.infrastructure.database import bootstrap as db_bootstrap
    from albuswall.infrastructure.task import bootstrap as task_bootstrap
    from albuswall.repositories import registry_repository
    from albuswall.service import register_service
    from albuswall.service.importer import ImportConfig
    from albuswall.service.source.trash.config import SourceTrashSettings

    # ``tests/test_configue/conftest.py`` 会清空 ``registry._CLASSES``，跑完整
    # 套件时导入期登记的 ``@configclass`` 已经不在了；这里按原声明幂等重建。
    configclass("db", "main")(db_bootstrap.Config)
    configclass("task", "main")(task_bootstrap.Config)
    configclass("import", "main")(ImportConfig)
    # 回收站的 [source_trash] 段同理（``Configue`` 缺席时会退回声明默认值，
    # 但这里补上更接近生产：配置真的被读，而不是走兜底 + WARNING）。
    configclass("source_trash", "main")(SourceTrashSettings)

    loop = HeadlessMainLoop()
    app = CoreApplication(main_loop=loop)
    container = app.container
    container.set("ipc_service", _IpcStub())

    setup_config(
        container,
        base_dir=tmp_path / "cfg",
        file_name="config.ini",
        ignore_missing=True,
        portable=True,
    )
    app.resolve_slot("config")
    app.resolve_slot("plugins")

    # ↓ 与 main.py phase 4 一致：主循环进容器，服务才会真的跨线程调度
    container.set("main_loop", loop)

    # 用**新的**注册表实例：进程级 ``get_default_registry()`` 一旦 freeze 就
    # 不能复用（见 tests/conftest.py 的硬约束），多个用例会互相干扰。
    # 生产里 ``registry_command_registry`` 只是把那个单例塞进同一个 key。
    container.register(
        "command_registry", CommandRegistry, returns=CommandRegistry,
    )
    register_database(container)
    register_task_service(container)
    register_trigger_service(container)
    # 回收站在 on_boot 构造时会向 Reconciler 注册 purge 条目，缺 key 直接
    # MissingDependencyError —— 生产里这一步由 main.py 的 infrastructure 阶段做。
    register_reconciler(container)
    registry_repository(container)
    register_service(container)
    return app, loop


class _LogSpy(logging.Handler):
    """收集日志：``Signal.emit`` 吞掉 slot 异常，只能靠日志发现状态层炸了。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def command_app(tmp_path, clean_hooks):
    """真装配（含 ``handlers/source.py`` 的 ``source.create``）+ freeze 的应用。"""
    from albuswall.plugins.command import _assemble_commands

    app, loop = _build_app(tmp_path)
    _assemble_commands(app.container)  # 插件在 on_loop 里做的事：装配 + freeze
    try:
        yield app, loop
    finally:
        # 幂等（``_is_finally`` 守卫）：跑过 exec 的用例不会重复执行 exit 钩子，
        # 没跑 exec 的用例靠这里停掉 TaskService 等后台线程 —— 否则残留线程会
        # 干扰同进程里 ``test_source_wiring`` 的"退出后无残留线程"断言。
        app.finally_()


def _run_with_worker(app, loop, worker) -> None:
    """在 ``on_loop`` 里起 worker 线程并跑完事件循环。

    **不在主线程 join worker**：主线程一阻塞，worker 通过
    ``MainLoopService`` 投递回来的工作就没人消费了（死锁的第一种形态）。
    """
    def _on_loop(_app) -> None:
        threading.Thread(target=worker, name="ipc-conn", daemon=True).start()

    Application.on_loop(_on_loop)
    app.exec()


def _source_dir() -> str:
    return tempfile.mkdtemp(prefix="cmdsrc-")


def _create_request(path: str) -> dict:
    """建一个**禁用**的源：不建扫描任务，测试因此不碰 sqlite 并发写。

    仍然走完整链路（写库 → ``source_created`` → 状态层对账），而
    ``disabled=True`` 让状态层走"剔除"分支 —— 那里同样有 ``TrackingState``
    的属主断言，所以线程归属照样被验证到。
    """
    return {
        "method": "source.create",
        "params": {"args": ["pics", path], "kwargs": {"disabled": True}},
    }


# ==================== 用例 ====================

def test_command_on_ipc_thread_but_state_work_on_main_thread(command_app):
    """handler 在 ``ipc-conn``，状态层写入在主循环线程，错误码也能回传。"""
    app, loop = command_app
    container = app.container
    ipc = container.require("ipc_service")
    source_service = container.require("source_service")

    seen: list[str] = []
    source_service.source_created.connect(
        lambda _sender, *, source_id: seen.append(
            threading.current_thread().name)
    )

    evidence: dict[str, Any] = {}

    def _worker() -> None:
        evidence["conn_thread"] = threading.current_thread().name
        evidence["reply"] = ipc.handler(_create_request(_source_dir()))
        try:
            ipc.handler({
                "method": "source.create",
                "params": {"args": ["bad", ""]},
            })
        except BaseException as exc:  # noqa: BLE001
            evidence["error"] = (type(exc).__name__, getattr(exc, "code", None))
        # 状态层快照必须在 shutdown 之前取（on_exit 会清空跟踪集）。
        # 禁用的源不该被跟踪 —— 但"没被跟踪"是状态层**主动**判定的结果，
        # 说明它确实在主线程上跑过一遍。
        evidence["tracked"] = sorted(
            container.require("source_state_service").tracked_sources
        )
        loop.quit(0)

    _run_with_worker(app, loop, _worker)

    assert evidence["conn_thread"] == "ipc-conn"      # handler 仍在子线程
    assert evidence["reply"] == {"id": 1}
    assert seen == ["MainThread"]                    # 服务的工作在主线程
    assert evidence["tracked"] == []                 # 禁用的源不被跟踪（状态层跑过）
    # 错误码经翻译层回传（而不是 CommandFailed / command_error）
    assert evidence["error"] == ("IpcRemoteError", "source_path_invalid")


def test_no_state_owner_error_is_ever_logged(command_app):
    """没有"状态层被跨线程写"的日志 —— 即 hop 生效（反向兜底）。"""
    app, loop = command_app
    ipc = app.container.require("ipc_service")
    evidence: dict[str, Any] = {}

    def _worker() -> None:
        evidence["reply"] = ipc.handler(_create_request(_source_dir()))
        loop.quit(0)

    spy = _LogSpy()
    root = logging.getLogger()
    root.addHandler(spy)
    try:
        _run_with_worker(app, loop, _worker)
    finally:
        root.removeHandler(spy)

    assert evidence["reply"] == {"id": 1}
    offenders = [m for m in spy.messages if STATE_OWNER_ERROR in m]
    assert offenders == [], f"状态层仍在子线程被写: {offenders}"


def test_registry_is_frozen_after_assembly(fresh_registry, fake_container):
    """装配完成即冻结：之后注册会抛 ``RegistryFrozen``（新增命令要改 MODULES）。

    这条不需要真 infra，用 conftest 的轻量 fixture 即可（少起一个真 DB / 任务池）。
    """
    from albuswall.command.exceptions import RegistryFrozen
    from albuswall.plugins.command import _assemble_commands

    container = fake_container
    container.set("command_registry", fresh_registry)

    _assemble_commands(container)

    assert fresh_registry.frozen is True
    with pytest.raises(RegistryFrozen):
        fresh_registry.register(Command(path="late.cmd", func=lambda: None))
