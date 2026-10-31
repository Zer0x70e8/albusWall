#
"""插件系统：声明、拓扑排序、三阶段生命周期。

守的契约（与 ``core/plugins.py`` 的模块 docstring 对应）：

* ``@plugin`` 只做**声明**——装饰时往 ``_pending`` 追加，工厂要到 ``setup``
  才执行；三种返回值形态（``None`` / ``(start, stop)`` / 带 ``.start``
  ``.stop`` 的对象）都要能被归一；
* ``PluginManager`` 是**实例级**的，四类失败语义各不相同（setup 回滚后抛、
  start 抛、stop 吞、``enabled``/``disabled`` 过滤规则照抄旧实现）；
* ``PluginContext`` 只记录不设防：``provide`` / ``on`` 的产物要能在 setup
  失败时被撤销，绕过 ``ctx`` 直接操作容器则不会被撤销（这条边界写进文档，
  这里不测）。
"""

from __future__ import annotations

import importlib.metadata
import logging
import types

import pytest

from albuswall.core.application import CoreApplication
from albuswall.core.plugins import (
    BasePlugin,
    DuplicatePluginError,
    MissingPluginError,
    PluginCycleError,
    PluginError,
    PluginManager,
    _pending,
    plugin,
)
from albuswall.core.registry import hooks_map
from albuswall.utils.hooks import run_hooks

# ==================== 辅助 ====================

def make_app() -> CoreApplication:
    """一个干净的、plugins 槽位已 resolve 的应用。

    ``CoreApplication.plugins`` 是**严格**属性：槽位没 resolve 就抛
    ``SlotNotResolvedError``（``main._boot`` 的 phase 2 同样先
    ``app.resolve_slot("plugins")`` 再 ``app.plugins()``）。测试要直连插件
    管理器，就得把宿主这一步补上——否则测的是「boot 顺序」而不是插件语义。
    """
    app = CoreApplication()
    app.resolve_slot("plugins")
    return app


def make_manager() -> PluginManager:
    """一个干净的应用 + 它的（空的）插件管理器。"""
    return make_app().plugins


class RecordingPlugin:
    """最小的协议式插件：按名字往共享日志里追加生命周期事件。"""

    def __init__(
            self,
            name: str,
            requires: tuple[str, ...] = (),
            log: list[str] | None = None,
    ) -> None:
        self.name = name
        self.version = "1.0.0"
        self.requires = tuple(requires)
        self.log = log if log is not None else []
        self.ctx = None

    def setup(self, ctx) -> None:
        self.ctx = ctx
        self.log.append(f"setup:{self.name}")

    def start(self) -> None:
        self.log.append(f"start:{self.name}")

    def stop(self) -> None:
        self.log.append(f"stop:{self.name}")


# ==================== @plugin 装饰器 ====================

def test_plugin_bare_form_uses_function_name():
    def sample(container):
        return None

    returned = plugin(sample)

    assert returned is sample                     # 返回原函数，保持可调用
    assert [p.name for p in _pending] == ["sample"]
    assert _pending[0].version == "0.0.0"
    assert _pending[0].requires == ()


def test_plugin_named_form_carries_metadata():
    @plugin("with-meta", version="2.3.4", requires=("base",))
    def factory(container):
        return None

    assert callable(factory)
    declared = _pending[-1]
    assert declared.name == "with-meta"
    assert declared.version == "2.3.4"
    assert declared.requires == ("base",)


def test_plugin_rejects_unsupported_argument():
    with pytest.raises(TypeError):
        plugin(42)  # type: ignore[arg-type]


def test_pending_snapshot_is_independent_of_drain():
    """``load_pending`` 取走的是快照，并且清空 ``_pending``。"""
    manager = make_manager()

    @plugin("drained")
    def factory(container):
        return None

    assert len(_pending) == 1
    assert manager.load_pending() == 1
    assert _pending == []
    assert "drained" in manager
    assert manager.load_pending() == 0


# ==================== 工厂返回值 → 生命周期 ====================

def test_factory_returning_none_has_no_lifecycle():
    manager = make_manager()
    calls: list[str] = []

    @plugin("none-plugin")
    def factory(container):
        calls.append("factory")
        return None

    manager.load_pending()
    manager.setup_all()
    manager.start_all()
    manager.stop_all()

    assert calls == ["factory"]


def test_factory_returning_tuple_gets_start_and_stop():
    manager = make_manager()
    calls: list[str] = []

    @plugin("tuple-plugin")
    def factory(container):
        return (lambda: calls.append("start"), lambda: calls.append("stop"))

    manager.load_pending()
    manager.setup_all()
    manager.start_all()
    manager.stop_all()

    assert calls == ["start", "stop"]


def test_factory_returning_object_uses_its_methods():
    manager = make_manager()
    calls: list[str] = []

    class Service:
        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    @plugin("object-plugin")
    def factory(container):
        return Service()

    manager.load_pending()
    manager.setup_all()
    manager.start_all()
    manager.stop_all()

    assert calls == ["start", "stop"]


def test_factory_returning_other_value_has_no_lifecycle():
    manager = make_manager()

    @plugin("plain-plugin")
    def factory(container):
        return 42

    manager.load_pending()
    manager.setup_all()
    manager.start_all()
    manager.stop_all()          # 不抛即可


def test_factory_receives_container_not_context():
    """工厂签名统一为 ``Callable[[Container], Any]``：拿到的是容器。"""
    manager = make_manager()
    seen: list[object] = []

    @plugin("container-plugin")
    def factory(container):
        seen.append(container)
        return None

    manager.load_pending()
    manager.setup_all()

    assert seen == [manager._app.container]


# ==================== 收集 ====================

def test_add_rejects_duplicate_name():
    manager = make_manager()
    manager.add(RecordingPlugin("dup"))
    with pytest.raises(DuplicatePluginError):
        manager.add(RecordingPlugin("dup"))


def test_add_rejects_empty_name():
    manager = make_manager()
    with pytest.raises(PluginError):
        manager.add(RecordingPlugin(""))


def test_manager_is_instance_level():
    """两个 ``CoreApplication`` 的插件表互不干扰（不是类级单例）。"""
    first = make_manager()
    second = make_manager()
    first.add(RecordingPlugin("only-first"))

    assert "only-first" in first
    assert "only-first" not in second


def test_dunder_protocols():
    manager = make_manager()
    manager.extend([RecordingPlugin("a"), RecordingPlugin("b")])

    assert len(manager) == 2
    assert list(manager) == ["a", "b"]
    assert manager.get("a").name == "a"
    with pytest.raises(KeyError):
        manager.get("missing")


# ==================== 拓扑排序 ====================

def test_topo_order_without_dependencies_keeps_declaration_order():
    manager = make_manager()
    manager.extend([RecordingPlugin("a"), RecordingPlugin("b")])

    assert manager._topo_order() == ["a", "b"]


def test_topo_order_puts_dependency_first():
    manager = make_manager()
    manager.extend([RecordingPlugin("a", requires=("b",)), RecordingPlugin("b")])

    assert manager._topo_order() == ["b", "a"]


def test_topo_order_chain():
    manager = make_manager()
    manager.extend([
        RecordingPlugin("c", requires=("b",)),
        RecordingPlugin("a", requires=("b",)),
        RecordingPlugin("b"),
    ])

    # 同层按声明顺序追加：c 先拜访（先收 b），再 a
    assert manager._topo_order() == ["b", "c", "a"]


def test_topo_order_detects_cycle():
    manager = make_manager()
    manager.extend([
        RecordingPlugin("a", requires=("b",)),
        RecordingPlugin("b", requires=("a",)),
    ])

    with pytest.raises(PluginCycleError):
        manager._topo_order()


def test_topo_order_detects_self_cycle():
    manager = make_manager()
    manager.add(RecordingPlugin("a", requires=("a",)))

    with pytest.raises(PluginCycleError):
        manager._topo_order()


def test_topo_order_missing_requirement():
    manager = make_manager()
    manager.add(RecordingPlugin("a", requires=("ghost",)))

    with pytest.raises(MissingPluginError):
        manager._topo_order()


def test_missing_requirement_is_also_key_error():
    """``MissingPluginError`` 同时是 ``KeyError``。"""
    assert issubclass(MissingPluginError, KeyError)


# ==================== setup_all 的过滤 ====================

def test_setup_all_runs_everything_by_default():
    manager = make_manager()
    log: list[str] = []
    manager.extend([RecordingPlugin("a", log=log), RecordingPlugin("b", log=log)])

    manager.setup_all()

    assert log == ["setup:a", "setup:b"]
    assert manager.setup_done is True


def test_setup_all_enabled_is_whitelist():
    manager = make_manager()
    log: list[str] = []
    manager.extend([RecordingPlugin("a", log=log), RecordingPlugin("b", log=log)])

    manager.setup_all(enabled=["a"])

    assert log == ["setup:a"]


def test_setup_all_disabled_skips_listed():
    manager = make_manager()
    log: list[str] = []
    manager.extend([RecordingPlugin("a", log=log), RecordingPlugin("b", log=log)])

    manager.setup_all(disabled=["a"])

    assert log == ["setup:b"]


def test_setup_all_accepts_dict_for_disabled():
    """``dict`` 按 keys 处理（配置段视图就是这个类型）。"""
    manager = make_manager()
    log: list[str] = []
    manager.extend([RecordingPlugin("a", log=log), RecordingPlugin("b", log=log)])

    manager.setup_all(disabled={"a": True})

    assert log == ["setup:b"]


def test_setup_all_enabled_unknown_name_raises():
    manager = make_manager()
    manager.add(RecordingPlugin("a"))

    with pytest.raises(KeyError):
        manager.setup_all(enabled=["nope"])


def test_setup_all_disabled_unknown_name_only_logs(caplog):
    manager = make_manager()
    log: list[str] = []
    manager.add(RecordingPlugin("a", log=log))

    with caplog.at_level(logging.DEBUG, logger="albuswall.core"):
        manager.setup_all(disabled=["nope"])

    assert log == ["setup:a"]
    assert any("unknown plugins" in record.getMessage() for record in caplog.records)


def test_setup_all_rejects_both_filters():
    manager = make_manager()
    manager.add(RecordingPlugin("a"))

    with pytest.raises(ValueError):
        manager.setup_all(enabled=["a"], disabled=["a"])


def test_setup_all_is_not_repeatable():
    manager = make_manager()
    manager.add(RecordingPlugin("a"))
    manager.setup_all()

    with pytest.raises(PluginError):
        manager.setup_all()


def test_setup_order_follows_topology():
    manager = make_manager()
    log: list[str] = []
    manager.extend([
        RecordingPlugin("late", requires=("early",), log=log),
        RecordingPlugin("early", log=log),
    ])

    manager.setup_all()

    assert log == ["setup:early", "setup:late"]


# ==================== start / stop 顺序 ====================

def test_start_and_stop_follow_topology_in_opposite_directions():
    manager = make_manager()
    log: list[str] = []
    manager.extend([
        RecordingPlugin("late", requires=("early",), log=log),
        RecordingPlugin("early", log=log),
    ])

    manager.setup_all()
    manager.start_all()
    manager.stop_all()

    assert log == [
        "setup:early", "setup:late",
        "start:early", "start:late",
        "stop:late", "stop:early",
    ]
    assert manager.started is True


def test_start_all_requires_setup():
    manager = make_manager()
    manager.add(RecordingPlugin("a"))

    with pytest.raises(PluginError):
        manager.start_all()


def test_start_all_skips_filtered_plugins():
    manager = make_manager()
    log: list[str] = []
    manager.extend([RecordingPlugin("a", log=log), RecordingPlugin("b", log=log)])

    manager.setup_all(enabled=["a"])
    manager.start_all()

    assert log == ["setup:a", "start:a"]


# ==================== 失败语义 ====================

class _FailingPlugin(RecordingPlugin):
    """``setup`` 抛异常的插件；同时通过 ctx 注册了服务与 hook。"""

    def setup(self, ctx) -> None:
        ctx.provide("failing_service", lambda: "boom")
        ctx.on("init")(lambda: None)
        raise RuntimeError("setup exploded")


def test_setup_failure_rolls_back_and_stops_previous_in_reverse(caplog):
    manager = make_manager()
    log: list[str] = []
    first = RecordingPlugin("first", log=log)
    manager.extend([first, _FailingPlugin("boom", log=log)])

    container = manager._app.container
    container.set("failing_service", "pre-existing")

    with pytest.raises(RuntimeError):
        manager.setup_all()

    # 已 setup 的插件被逆序 stop（这里只有一个）
    assert log == ["setup:first", "stop:first"]
    # 本插件的容器注册被撤销（但它覆盖过已存在的同名依赖，不会「复原」）
    assert "failing_service" not in container
    # 本插件的 hook 被撤销
    assert all(
        not getattr(hook, "__name__", "").startswith("<lambda>")
        for hook in hooks_map["init"]
    )
    # 状态回退：还能重来
    assert manager.setup_done is False


def test_setup_failure_propagates_original_exception():
    manager = make_manager()
    manager.add(_FailingPlugin("boom"))

    with pytest.raises(RuntimeError, match="setup exploded"):
        manager.setup_all()


def test_setup_rollback_unregisters_hooks_and_services():
    manager = make_manager()
    container = manager._app.container
    plugin_ = _FailingPlugin("boom")
    manager.add(plugin_)

    before_init = list(hooks_map["init"])
    with pytest.raises(RuntimeError):
        manager.setup_all()

    assert hooks_map["init"] == before_init
    assert "failing_service" not in container


class _FailingStartPlugin(RecordingPlugin):
    def start(self) -> None:
        raise RuntimeError("start exploded")


def test_start_failure_propagates_and_keeps_plugin_in_started():
    manager = make_manager()
    log: list[str] = []
    manager.extend([
        RecordingPlugin("ok", log=log),
        _FailingStartPlugin("bad", log=log),
    ])

    manager.setup_all()
    with pytest.raises(RuntimeError, match="start exploded"):
        manager.start_all()

    assert manager._started == ["ok", "bad"]
    assert manager.started is False

    manager.stop_all()                  # 主机侧 finally_ 会调它
    assert "stop:bad" in log and "stop:ok" in log


class _FailingStopPlugin(RecordingPlugin):
    def stop(self) -> None:
        raise RuntimeError("stop exploded")


def test_stop_failure_is_swallowed_and_logged(caplog):
    manager = make_manager()
    log: list[str] = []
    manager.extend([
        RecordingPlugin("first", log=log),
        _FailingStopPlugin("bad", log=log),
    ])

    manager.setup_all()
    manager.start_all()

    with caplog.at_level(logging.ERROR, logger="albuswall.core"):
        manager.stop_all()               # 不抛

    assert manager._started == []
    assert any("stop exploded" in record.getMessage()
               or record.exc_info is not None
               for record in caplog.records)
    # 逆序：先 bad 后 first；bad 失败不影响 first
    assert "stop:first" in log


def test_slow_setup_emits_warning(monkeypatch, caplog):
    import albuswall.core.plugins as plugins_module

    class _Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def perf_counter(self) -> float:
            self.value += 0.5            # 每次调用前进 500ms
            return self.value

    monkeypatch.setattr(plugins_module, "time", _Clock())

    manager = make_manager()
    manager.add(RecordingPlugin("slow"))

    with caplog.at_level(logging.WARNING, logger="albuswall.core"):
        manager.setup_all()

    warnings = [r for r in caplog.records if "setup took" in r.getMessage()]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert warnings[0].levelno == logging.WARNING


def test_slow_threshold_is_instance_level():
    assert make_manager().setup_slow_threshold_ms == 200.0


# ==================== PluginContext ====================

def test_ctx_on_loop_wraps_zero_arg_callable():
    """``ctx.on("loop")`` 装的是零参函数，运行时不会 ``TypeError``。"""
    manager = make_manager()
    calls: list[str] = []

    class LoopPlugin(BasePlugin):
        name = "looper"

        def setup(self, ctx) -> None:
            @ctx.on("loop")
            def on_loop() -> None:
                calls.append("loop")

    manager.add(LoopPlugin())
    manager.setup_all()

    run_hooks(hooks_map["loop"], args=(manager._app,))
    assert calls == ["loop"]


def test_ctx_on_passes_through_non_loop_hooks():
    manager = make_manager()
    calls: list[str] = []

    class InitPlugin(BasePlugin):
        name = "initer"

        def setup(self, ctx) -> None:
            @ctx.on("init")
            def on_init() -> None:
                calls.append("init")

    manager.add(InitPlugin())
    manager.setup_all()

    run_hooks(hooks_map["init"])
    assert calls == ["init"]


def test_ctx_provide_and_require():
    manager = make_manager()
    container = manager._app.container

    class ProvidePlugin(BasePlugin):
        name = "provider"

        def setup(self, ctx) -> None:
            ctx.provide("answer", lambda _c: 42)
            assert ctx.require("answer") == 42
            assert ctx.get("missing", "fallback") == "fallback"

    manager.add(ProvidePlugin())
    manager.setup_all()

    assert container.require("answer") == 42


def test_ctx_rollback_unregisters_provided_service_and_hook():
    manager = make_manager()
    container = manager._app.container
    before = list(hooks_map["boot"])
    captured = {}

    class RollbackPlugin(BasePlugin):
        name = "rollback"

        def setup(self, ctx) -> None:
            ctx.provide("temp_service", lambda _c: "temp")

            @ctx.on("boot")
            def on_boot() -> None:
                captured["called"] = True

            ctx._rollback()

    manager.add(RollbackPlugin())
    manager.setup_all()

    assert "temp_service" not in container
    assert hooks_map["boot"] == before


def test_ctx_log_property_namespaces_by_plugin():
    manager = make_manager()
    names: list[str] = []

    class LogPlugin(BasePlugin):
        name = "logger-plugin"

        def setup(self, ctx) -> None:
            names.append(ctx.log.name)

    manager.add(LogPlugin())
    manager.setup_all()

    assert names == ["albuswall.plugins.logger-plugin"]


# ==================== 发现层入口 ====================

def test_load_builtin_imports_package_modules(tmp_path, monkeypatch):
    pkg = tmp_path / "albuswall_test_builtin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text(
        "from albuswall.core.plugins import plugin\n"
        "\n"
        "@plugin('builtin-alpha', version='2.0.0')\n"
        "def alpha(container):\n"
        "    return None\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    manager = make_manager()
    assert manager.load_builtin("albuswall_test_builtin") == 1
    assert manager.get("builtin-alpha").version == "2.0.0"


def test_load_builtin_tolerates_missing_package():
    manager = make_manager()
    assert manager.load_builtin("albuswall_no_such_plugins") == 0


def test_load_directory_imports_user_scripts(tmp_path):
    script = tmp_path / "my_plugin.py"
    script.write_text(
        "from albuswall.core.plugins import plugin\n"
        "\n"
        "@plugin('user-plugin')\n"
        "def user(container):\n"
        "    return None\n"
    )
    manager = make_manager()

    assert manager.load_directory(tmp_path) == 1
    assert "user-plugin" in manager


def test_load_directory_skips_missing_dir(tmp_path):
    manager = make_manager()
    assert manager.load_directory(tmp_path / "nope") == 0


def test_load_entry_points_callable_target(monkeypatch):
    calls: list[str] = []

    def declare() -> None:
        @plugin("entry-point-plugin")
        def factory(container):
            calls.append("called")
            return None

    class _EntryPoint:
        name = "fake-ep"

        def load(self):
            return declare

    def fake_entry_points(*, group):
        assert group == "albuswall.plugins"
        return [_EntryPoint()]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

    manager = make_manager()
    assert manager.load_entry_points() == 1
    assert "entry-point-plugin" in manager

    # 发现层只负责声明：工厂要到 setup 才执行
    assert calls == []
    manager.setup_all()
    assert calls == ["called"]


def test_load_entry_points_module_target_is_noop(monkeypatch):
    module = types.ModuleType("albuswall_fake_ep_module")

    class _EntryPoint:
        name = "fake-module-ep"

        def load(self):
            return module

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [_EntryPoint()],
    )

    manager = make_manager()
    assert manager.load_entry_points() == 0
