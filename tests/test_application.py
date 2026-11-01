#
"""``CoreApplication.exec`` 的阶段顺序与插件接线。

这里用假 ``MainLoop`` 记录调用序列，锁住四条契约：

* 正常路径的顺序是 ``setup → boot → init → start → loop → run → stop → exit``；
* 宿主已经在 boot 阶段自己 ``setup_all`` / ``start_all`` 过时，``exec`` 不再
  重复调用（条件调用，而不是无条件）；
* ``start`` 失败时异常照常传出，但 ``finally_`` 已经停过插件；
* ``exit`` hook 抛异常只记日志（退出路径不允许崩）。
"""

from __future__ import annotations

import logging
import types

import pytest

from albuswall.core.application import Application, CoreApplication
from albuswall.core.exit_codes import ExitCode
from albuswall.core.main_loop import MainLoop
from albuswall.core.plugins import BasePlugin
from albuswall.core.registry import register

# ==================== 辅助 ====================

class FakeLoop(MainLoop):
    """只记录 ``run`` 的假主循环。"""

    def __init__(self, log: list[str], code: int = 0) -> None:
        self.log = log
        self.code = code
        self.quit_code = None

    def run(self) -> int:
        self.log.append("run")
        return self.code

    def quit(self, code=None) -> None:
        self.quit_code = code


class StepPlugin(BasePlugin):
    """把生命周期写进共享日志的插件。"""

    name = "step"
    version = "0.0.0"
    requires: tuple[str, ...] = ()

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def setup(self, ctx) -> None:
        self.log.append("setup")

    def start(self) -> None:
        self.log.append("start")

    def stop(self) -> None:
        self.log.append("stop")


class FailingStartPlugin(StepPlugin):
    name = "failing"

    def start(self) -> None:
        self.log.append("start")
        raise RuntimeError("start exploded")


class _StubConfig:
    """``config`` 槽位的最小桩：``exec`` 只要求容器里有 ``config`` 可取。"""

    tree = types.SimpleNamespace(main={})


def make_app(main_loop: MainLoop | None = None) -> CoreApplication:
    """装配一个「boot 过一半」的应用：``config`` / ``plugins`` 槽位已 resolve。

    真实启动里这两步在 ``main._boot`` 的 phase 1/2 完成；``exec`` 里的
    ``resolve_slot`` 是幂等的，测试自己调一次即可。不调的话
    ``app.plugins`` 会抛 ``SlotNotResolvedError``、``exec`` 会因容器里没有
    ``config`` 抛 ``MissingDependencyError``——那测的是 boot 顺序，不是这里
    要守的 exec 契约。
    """
    app = CoreApplication(main_loop=main_loop)
    app.container.set("config", _StubConfig())
    app.resolve_slot("config")
    app.resolve_slot("plugins")
    return app


# ==================== 阶段顺序 ====================

def test_exec_runs_stages_in_order():
    log: list[str] = []
    app = make_app(FakeLoop(log))
    app.plugins.add(StepPlugin(log))
    register("boot", lambda: log.append("boot"))
    register("init", lambda: log.append("init"))
    register("loop", lambda _app: log.append("loop"))
    register("exit", lambda: log.append("exit"))

    assert app.exec() == ExitCode.OK

    assert log == [
        "setup", "boot", "init", "start", "loop", "run", "stop", "exit",
    ]


def test_exec_skips_setup_and_start_when_host_already_did_them():
    log: list[str] = []
    app = make_app(FakeLoop(log))
    app.plugins.add(StepPlugin(log))
    app.plugins.setup_all()
    app.plugins.start_all()

    log.clear()
    app.exec()

    # 只有 boot/init/loop/run/stop/exit；setup/start 不再重复
    assert log == ["run", "stop"]


def test_exec_returns_loop_exit_code():
    log: list[str] = []
    app = make_app(FakeLoop(log, code=ExitCode.OS_ERROR))

    assert app.exec() == ExitCode.OS_ERROR


def test_exec_maps_unknown_int_to_pseudo_exit_code():
    log: list[str] = []
    app = make_app(FakeLoop(log, code=143))

    assert int(app.exec()) == 143


# ==================== finally_ ====================

def test_finally_is_idempotent():
    log: list[str] = []
    app = make_app(FakeLoop(log))
    app.plugins.add(StepPlugin(log))
    register("exit", lambda: log.append("exit"))

    app.exec()
    app.finally_()

    assert log.count("exit") == 1
    assert log.count("stop") == 1


def test_start_failure_propagates_and_plugins_are_stopped():
    log: list[str] = []
    app = make_app(FakeLoop(log))
    app.plugins.add(FailingStartPlugin(log))

    with pytest.raises(RuntimeError, match="start exploded"):
        app.exec()

    assert log == ["setup", "start", "stop"]


def test_exit_hook_failure_is_logged_not_raised(caplog):
    log: list[str] = []
    app = make_app(FakeLoop(log))

    def broken_exit() -> None:
        raise RuntimeError("exit exploded")

    register("exit", broken_exit)

    with caplog.at_level(logging.ERROR, logger="albuswall.core"):
        assert app.exec() == ExitCode.OK

    messages = [record.getMessage() for record in caplog.records]
    assert any("exit hook failed" in msg for msg in messages), messages


def test_exit_hook_failure_sets_finally_flag():
    log: list[str] = []
    app = make_app(FakeLoop(log))
    register("exit", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    app.exec()

    assert app._is_finally is True


# ==================== Application 门面 ====================

def test_application_plugins_returns_instance_manager(monkeypatch):
    app = make_app(FakeLoop([]))
    monkeypatch.setattr(Application, "_instance", app)

    assert Application.plugins() is app.plugins


def test_application_load_plugins_forwards_group(monkeypatch):
    app = make_app(FakeLoop([]))
    monkeypatch.setattr(Application, "_instance", app)
    groups: list[str] = []

    def fake_load_entry_points(self, group: str = "albuswall.plugins") -> int:
        groups.append(group)
        return 0

    monkeypatch.setattr(
        type(app.plugins), "load_entry_points", fake_load_entry_points,
    )

    manager = Application.load_plugins(group="custom.group")

    assert manager is app.plugins
    assert groups == ["custom.group"]


# ==================== _boot 读 [plugin_disables] ====================

class _FakeTree:
    """只实现「段存在性 + 段视图可迭代」的最小配置树桩。"""

    def __init__(self, sections: dict[str, list[str]]) -> None:
        self._sections = sections

    def __contains__(self, name: object) -> bool:
        return name in self._sections

    def __getitem__(self, name: str) -> list[str]:
        return self._sections[name]


class _FakeConfig:
    def __init__(self, sections: dict[str, list[str]]) -> None:
        self.tree = types.SimpleNamespace(main=_FakeTree(sections))


def test_plugin_disables_reads_section_keys():
    from albuswall.main import _plugin_disables

    config = _FakeConfig({"plugin_disables": ["builtin_ui", "ipc"]})

    assert _plugin_disables(config) == {"builtin_ui", "ipc"}


def test_plugin_disables_missing_section_is_empty():
    from albuswall.main import _plugin_disables

    assert _plugin_disables(_FakeConfig({})) == set()


def test_plugin_disables_tolerates_missing_config():
    from albuswall.main import _plugin_disables

    assert _plugin_disables(None) == set()
