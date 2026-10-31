#
"""内置示例插件 ``albuswall.plugins.hello`` 的回归。

守四件事：

1. **声明只在 import 期发生一次**：模块被 import 时 `@plugin` 追加到
   `_pending`；重复 import（`sys.modules` 命中）不会重复声明。所以每个用例
   开头都先 `sys.modules.pop(...)` 再造「首次 import」；
2. **发现 + 收编 + 生命周期**：`load_builtin` 收编后，`exec` 的真实顺序是
   `boot → init → start → loop → stop → exit`，且 `stop` 早于 `exit`；
3. **服务可见性**：串插件通过容器拿到 `hello_service`；
4. **可禁用 / 可配置**：`disabled=["hello"]` 后不注册任何东西；
   `[hello] greeting` 能覆盖默认问候语（走 `@configclass` 类视图，读的是全局
   `Configue` 单例，所以这些用例造的是**真配置**，不是容器里的假对象）。
"""

from __future__ import annotations

import importlib
import logging
import sys
from contextlib import suppress

import pytest

from albuswall.configue import (
    _LOAD_HOOKS,
    _PENDING_CONVERTERS,
    ConfigFile,
    Configue,
    reg,
)
from albuswall.configue.registry import _CLASSES, _FIELD_OWNER, _FILES, _PATHS
from albuswall.core.application import CoreApplication
from albuswall.core.main_loop import MainLoop
from albuswall.core.plugins import _pending
from albuswall.core.registry import hooks_map

HELLO_MODULE = "albuswall.plugins.hello"
HELLO_LOGGER = "albuswall.plugins.hello"


# ==================== 辅助 ====================

def _fresh_import() -> types.ModuleType:
    """重新 import hello，模拟「首次 import 触发模块级 @plugin」。"""
    sys.modules.pop(HELLO_MODULE, None)
    return importlib.import_module(HELLO_MODULE)


class _FakeLoop(MainLoop):
    def __init__(self) -> None:
        self.code = 0
        self.ran = False

    def run(self) -> int:
        self.ran = True
        return self.code

    def quit(self, code=None) -> None:
        pass


class _PlaceholderConfig:
    """占位 config：``exec`` 只需要 ``config`` 槽位里有东西。

    插件读配置走 ``@configclass`` 类视图（``HelloConf`` → 全局 ``Configue``
    单例），**不再**从容器里的 config 对象取，所以这里不需要假配置树。
    """


@pytest.fixture
def real_config(tmp_path):
    """造一个真实的 ``Configue`` 单例，返回 ``make(ini_text) -> Configue``。

    类视图读的是**全局单例**（``registry._get_configue()``），容器里塞假对象
    已经影响不到它，所以「配置覆盖问候语」这类用例必须造真配置。

    收尾清空进程级全局表：``configue`` 的登记表是进程级的，不清会跨用例串味
    （与 ``tests/test_configue/conftest.py`` 同一套复位步骤）。
    """

    created: list[Configue] = []

    def make(ini_text: str | None = None) -> Configue:
        if ini_text is not None:
            (tmp_path / "config.ini").write_text(ini_text, encoding="utf-8")
        reg(
            ConfigFile.INI, "config.ini", name="main",
            autosave="off", optional=True,
        )
        config = Configue(tmp_path)
        config.load()
        created.append(config)
        return config

    try:
        yield make
    finally:
        for config in created:
            with suppress(Exception):
                config.close()
        _FILES.clear()
        _CLASSES.clear()
        _PATHS.clear()
        _FIELD_OWNER.clear()
        _LOAD_HOOKS.clear()
        _PENDING_CONVERTERS.clear()
        Configue._instance = None


def make_app(main_loop: MainLoop | None = None, *,
             config: object | None = None) -> CoreApplication:
    """建应用并把 ``plugins`` 槽位 resolve 掉（等价 ``main._boot`` 的 phase 2）。

    ``CoreApplication.plugins`` 未 resolve 时会抛 ``SlotNotResolvedError``；
    ``config`` 只有走 ``exec`` 的用例需要传递——``exec`` 第一步就是
    ``resolve_slot("config")``，容器里没它就会 ``MissingDependencyError``。
    """
    app = CoreApplication(main_loop=main_loop)
    if config is not None:
        app.container.set("config", config)
    app.resolve_slot("plugins")
    return app


def _hello_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == HELLO_LOGGER
    ]


# ==================== 声明 / 发现 ====================

def test_hello_declares_itself_on_import():
    _fresh_import()

    names = [p.name for p in _pending]
    assert "hello" in names
    declared = next(p for p in _pending if p.name == "hello")
    assert declared.version == "1.0.0"
    assert declared.requires == ()


def test_reimport_does_not_duplicate_declaration():
    """模块已在 ``sys.modules`` 时再来一次 import 不会重复声明。"""
    _fresh_import()
    count_before = sum(1 for p in _pending if p.name == "hello")

    importlib.import_module(HELLO_MODULE)       # 命中缓存，模块体不重跑

    count_after = sum(1 for p in _pending if p.name == "hello")
    assert count_after == count_before


def test_load_builtin_discovers_hello():
    _fresh_import()
    manager = make_app().plugins

    assert manager.load_builtin("albuswall.plugins") >= 1
    assert "hello" in manager
    assert manager.get("hello").version == "1.0.0"


# ==================== setup 阶段的效果 ====================

def test_setup_registers_service_and_hooks():
    _fresh_import()
    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    service = app.container.require("hello_service")
    assert service.greeting == "Hello, AlbusWall!"
    assert service.started is False
    assert service.greet("there") == "Hello, AlbusWall! there"
    assert service.calls == 1

    for event in ("boot", "init", "loop", "exit"):
        assert hooks_map[event], f"hello 应该挂上 {event} hook"


def test_service_is_visible_to_other_code():
    """别的插件（这里用一条普通 loop hook 代替）能从容器拿到 hello_service。"""
    _fresh_import()
    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    seen = []
    hooks_map["loop"].append(lambda _app: seen.append(
        app.container.require("hello_service").greet("from another plugin")
    ))

    for hook in list(hooks_map["loop"]):
        hook(app)

    assert "Hello, AlbusWall! from another plugin" in seen


def test_disabled_hello_registers_nothing():
    _fresh_import()
    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all(disabled=["hello"])

    assert "hello_service" not in app.container
    assert not hooks_map["boot"]
    assert not hooks_map["init"]


# ==================== 完整生命周期 ====================

def test_exec_runs_hello_lifecycle_in_order(caplog):
    _fresh_import()
    loop = _FakeLoop()
    app = make_app(loop, config=_PlaceholderConfig())
    app.plugins.load_builtin("albuswall.plugins")

    with caplog.at_level(logging.INFO, logger=HELLO_LOGGER):
        assert app.exec() == 0

    assert loop.ran is True
    prefixes = [
        msg.split(":")[0]
        for msg in _hello_messages(caplog)
        if msg.startswith("[hello]")
    ]
    assert prefixes == [
        "[hello] boot",
        "[hello] init",
        "[hello] start",
        "[hello] loop",
        "[hello] stop",
        "[hello] exit",
    ], prefixes

    loop_messages = [m for m in _hello_messages(caplog) if "loop" in m]
    assert "Hello, AlbusWall! from the main loop" in loop_messages[0]


def test_started_flag_toggles_across_lifecycle():
    _fresh_import()
    app = make_app(_FakeLoop())
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    service = app.container.require("hello_service")
    assert service.started is False

    app.plugins.start_all()
    assert service.started is True

    app.plugins.stop_all()
    assert service.started is False


def test_exit_hook_is_idempotent():
    """exit 钩子会被 finally_ 跑一次；人工再跑一次不能炸。"""
    _fresh_import()
    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    for hook in list(hooks_map["exit"]):
        hook()
        hook()


# ==================== 配置覆盖问候语 ====================

def test_greeting_from_config(real_config):
    """``[hello] greeting`` 覆盖默认问候语（读的是 ``HelloConf`` 类视图）。"""
    _fresh_import()                       # 先 import：让 HelloConf 完成声明
    real_config("[hello]\ngreeting =   你好，AlbusWall  \n")

    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    assert app.container.require("hello_service").greeting == "你好，AlbusWall"


def test_greeting_falls_back_when_section_missing(real_config):
    _fresh_import()
    real_config("[log]\nlevel = INFO\n")     # 真配置，但没有 [hello]

    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    assert app.container.require("hello_service").greeting == "Hello, AlbusWall!"


def test_greeting_falls_back_on_empty_value(real_config):
    """空值 / 空白不算配置（strip 后为空 → 默认问候语）。"""
    _fresh_import()
    real_config("[hello]\ngreeting =    \n")

    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    assert app.container.require("hello_service").greeting == "Hello, AlbusWall!"


def test_greeting_falls_back_without_configue():
    """没有 ``Configue`` 单例时插件照样能起来（``ConfigMissingError`` 被吞）。"""
    _fresh_import()
    assert Configue._instance is None

    app = make_app()
    app.plugins.load_builtin("albuswall.plugins")
    app.plugins.setup_all()

    assert app.container.require("hello_service").greeting == "Hello, AlbusWall!"
