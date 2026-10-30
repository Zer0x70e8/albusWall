#
"""``configue1.Configue``：单例、check()、load()、unload()、flush()、close()。

覆盖范围对照实现 prompt 的「边界行为表」。

注意：``configue1`` 的登记表是进程全局的，而 conftest 的 autouse fixture 每个
用例都会清空它，因此配置类必须**在 fixture 里声明**，不能在模块顶层定义。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from albuswall.configue1 import (
    ConfigError,
    ConfigFile,
    ConfigMissingError,
    Configue,
    LoadReport,
    configclass,
    field,
    reg,
)
from albuswall.configue1.registry import _CLASSES, _FILES

from .conftest import write_ini

# ==================== 辅助 ====================

MAIN_INI = "[core]\ndebug = true\nname = hello\nport = 1234\n"
MACHINE_INI = "[core]\nname = machine-wins\n"


def make_config(tmp_path: Path, **kwargs) -> Configue:
    write_ini(tmp_path / "config.ini", kwargs.pop("content", MAIN_INI))
    reg(ConfigFile.INI, "config.ini", name="main", **kwargs)
    return Configue(tmp_path)


def add_machine_file(tmp_path: Path, *, order: int = 20, **kwargs) -> None:
    write_ini(tmp_path / "machine.ini", kwargs.pop("content", MACHINE_INI))
    reg(ConfigFile.INI, "machine.ini", name="machine", order=order, **kwargs)


@pytest.fixture
def core():
    """每个用例一个全新的 ``Core`` 配置类（登记表会被清空）。"""

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False)
        name: str = field(default="default-name")
        port: int = field(default=8000)
        extra: str = field(default="default-extra")  # 文件里没有，走默认值

    return Core


# ==================== 单例 / base_dir ====================

def test_same_base_dir_returns_same_instance(tmp_path):
    a = Configue(tmp_path)
    b = Configue(tmp_path)
    assert a is b


def test_relative_base_dir_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = Configue(".")
    assert c.base_dir == tmp_path.resolve()

    monkeypatch.chdir(tmp_path.parent)
    assert c.base_dir == tmp_path.resolve()
    assert Configue(tmp_path) is c


def test_different_base_dir_raises(tmp_path):
    Configue(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(RuntimeError, match="already initialized"):
        Configue(other)


def test_tree_is_a_fresh_view_each_time(tmp_path):
    c = make_config(tmp_path)
    assert c.tree is not c.tree
    assert c.tree.main is not c.tree.main


def test_reg_and_configclass_are_proxies(tmp_path):
    c = Configue(tmp_path)
    c.reg(ConfigFile.INI, "x.ini", name="proxied", order=3)

    @c.configclass("proxied", "proxied")
    class Proxied:
        value: int = 1

    assert "proxied" in _FILES
    assert Proxied in _CLASSES


# ==================== check() ====================

def test_check_ok(tmp_path):
    c = make_config(tmp_path)
    report = c.check()
    assert isinstance(report, LoadReport)
    assert report.ok is True
    assert report.loaded_files == ()
    assert _FILES["main"].loaded is False  # check 不读盘、不改状态


def test_check_reports_unregistered_file(tmp_path):
    @configclass("core", "ghost")
    class Ghost:
        value: int = 1

    c = Configue(tmp_path)
    report = c.check()

    assert report.ok is False
    assert [(e.kind, e.file) for e in report.errors] == [
        ("unregistered_file", "ghost"),
    ]


def test_check_reports_missing_file(tmp_path):
    reg(ConfigFile.INI, "nope.ini", name="main", order=1)
    c = Configue(tmp_path)

    report = c.check()
    assert report.ok is False
    assert report.errors[0].kind == "file_not_found"
    assert report.errors[0].file == "main"


def test_check_reports_ambiguous_file(tmp_path):
    write_ini(tmp_path / "amb.conf", "[core]\n")
    write_ini(tmp_path / "amb.conf.ini", "[core]\n")
    reg(ConfigFile.INI, "amb.conf", name="amb")
    c = Configue(tmp_path)

    report = c.check()
    kinds = [e.kind for e in report.errors]
    assert "ambiguous_file" in kinds


# ==================== load() ====================

def test_load_reads_files_and_fills_report(tmp_path, core):
    c = make_config(tmp_path)
    report = c.load()

    assert report.ok is True
    assert report.loaded_files == ("main",)
    assert report.skipped_files == ()
    assert report.errors == ()

    assert _FILES["main"].loaded is True
    assert _FILES["main"].resolved_path == (tmp_path / "config.ini").resolve()


def test_load_order_high_wins(tmp_path, core):
    c = make_config(tmp_path, order=10)
    add_machine_file(tmp_path, order=20)
    c.load()

    assert core().name == "machine-wins"
    # definitions 是「低 → 高」
    defs = c.resolver.definitions_of("core", "name")
    assert defs[0].name == "config.ini"
    assert defs[-1].name == "machine.ini"


def test_load_order_independent_of_registration_order(tmp_path, core):
    # machine 先 reg，但 order 更大 → 仍然赢
    add_machine_file(tmp_path, order=20)
    make_config(tmp_path, order=10)
    Configue(tmp_path).load()

    assert core().name == "machine-wins"


def test_load_injects_defaults_only_when_absent(tmp_path, core):
    c = make_config(tmp_path)
    c.load()

    assert core().debug is True             # 文件里写了
    assert core().port == 1234              # 文件里写了（str -> int）
    assert core().extra == "default-extra"  # 文件里没写 → 默认值


def test_load_without_default_and_without_value_raises_config_missing(tmp_path):
    @configclass("core", "main")
    class NeedsValue:
        required: str

    c = make_config(tmp_path)
    c.load()

    with pytest.raises(ConfigMissingError) as ei:
        _ = NeedsValue().required
    assert ei.value.path == ("core", "required")
    assert ei.value.file == "main"
    # 继承 AttributeError，getattr 的默认值语义要好使
    assert getattr(NeedsValue(), "required", "fallback") == "fallback"


def test_access_before_load_raises_config_missing(tmp_path, core):
    make_config(tmp_path)
    Configue(tmp_path)

    with pytest.raises(ConfigMissingError, match="Configue.load"):
        _ = core().debug


def test_load_is_idempotent(tmp_path, core):
    c = make_config(tmp_path)
    c.load()
    core().port = 4242

    report = c.load()  # 不重新读盘，也不把 4242 冲掉
    assert report.loaded_files == ()
    assert report.skipped_files == ("main",)
    assert core().port == 4242


def test_load_picks_up_files_registered_later(tmp_path, core):
    """热插拔：第二次 load 只加载新登记的文件，且按 order 正确覆盖。"""
    c = make_config(tmp_path, order=10, autosave="off")
    c.load()
    assert core().name == "hello"

    write_ini(tmp_path / "hot.ini", "[core]\nname = hot\n")
    reg(ConfigFile.INI, "hot.ini", name="hot", order=30, autosave="off")

    report = c.load()

    assert report.loaded_files == ("hot",)
    assert report.skipped_files == ("main",)
    assert core().name == "hot"
    assert [p.name for p in c.resolver.definitions_of("core", "name")] == [
        "config.ini", "hot.ini",
    ]


def test_unload_of_overriding_file_falls_back_to_lower_priority(tmp_path, core):
    """卸载覆盖文件后，值回退到剩余定义文件里优先级最高的那个。"""
    c = make_config(tmp_path, order=10, autosave="off")
    add_machine_file(tmp_path, order=20, autosave="off")
    c.load()
    assert core().name == "machine-wins"

    c.unload("machine")

    assert core().name == "hello"
    assert c.resolver.origin_of("core", "name") == (
        tmp_path / "config.ini"
    ).resolve()
    # definitions 里只剩 main
    assert c.resolver.definitions_of("core", "name") == (
        (tmp_path / "config.ini").resolve(),
    )


def test_unload_of_only_contributor_removes_the_node(tmp_path, core):
    """唯一贡献文件被卸载 → 节点消失（没得回退）。"""
    c = make_config(tmp_path, order=10, autosave="off")
    c.load()

    c.unload("main")

    assert ("core", "name") not in c.resolver
    with pytest.raises(ConfigMissingError):
        _ = core().name


def test_unload_after_flush_keeps_file_value(tmp_path, core):
    """内存回退 ≠ 磁盘回滚：写过盘之后再 unload，文件里仍是新值。

    这是有意的语义，不是 bug——``unload`` 只撤 Resolver 的贡献，不碰磁盘。
    想让磁盘也回退，必须在 ``save()`` **之前** unload。
    """
    c = make_config(tmp_path, order=10, autosave="off")
    add_machine_file(tmp_path, order=20, autosave="off")
    c.load()
    assert core().name == "machine-wins"

    # 1. 经 machine 视图改值并落盘
    c.tree.machine.core.name = "user-edit"
    c.tree.machine.save()
    assert "user-edit" in (tmp_path / "machine.ini").read_text(encoding="utf-8")

    # 2. 卸载 machine → 内存回退到 main 的 hello
    c.unload("machine")
    assert core().name == "hello"

    # 3. 磁盘上 machine.ini 仍是 user-edit
    assert "user-edit" in (tmp_path / "machine.ini").read_text(encoding="utf-8")

    # 4. 重新加载 → 读到磁盘上的 user-edit（不是回退后的 hello）
    c.load()
    assert core().name == "user-edit"


def test_load_unregistered_file_raises(tmp_path):
    @configclass("core", "ghost")
    class Ghost:
        value: int = 1

    c = Configue(tmp_path)
    with pytest.raises(ConfigError, match="unregistered file"):
        c.load()


def test_load_missing_file_raises(tmp_path):
    reg(ConfigFile.INI, "nope.ini", name="main")
    c = Configue(tmp_path)
    with pytest.raises(ConfigError) as ei:
        c.load()
    assert "config file not found" in str(ei.value)


def test_load_ambiguous_file_raises(tmp_path):
    write_ini(tmp_path / "amb.conf", "[core]\n")
    write_ini(tmp_path / "amb.conf.ini", "[core]\n")
    reg(ConfigFile.INI, "amb.conf", name="amb")
    c = Configue(tmp_path)

    with pytest.raises(ConfigError) as ei:
        c.load()
    assert "ambiguous" in str(ei.value)


def test_load_type_check_failure(tmp_path):
    @configclass("core", "main")
    class Typed:
        port: int = 0

    c = make_config(tmp_path, content="[core]\nport = not-an-int\n")
    with pytest.raises(ConfigError, match="type check failed"):
        c.load()


def test_load_read_failed_for_unimplemented_backend(tmp_path):
    """TOML / YAML 没实现 → ``check()`` / ``load()`` 都报 read_failed。"""
    write_ini(tmp_path / "x.toml", "[core]\n")
    reg(ConfigFile.TOML, "x.toml", name="toml-file")
    c = Configue(tmp_path)

    report = c.check()
    assert report.ok is False
    assert report.errors[0].kind == "read_failed"
    assert "no backend registered" in report.errors[0].message

    with pytest.raises(ConfigError, match="no backend registered"):
        c.load()


def test_load_partial_returns_report_instead_of_raising(tmp_path, core):
    reg(ConfigFile.INI, "nope.ini", name="broken")
    c = make_config(tmp_path)

    report = c.load(partial=True)
    assert report.ok is False
    assert report.loaded_files == ("main",)
    assert {e.kind for e in report.errors} == {"file_not_found"}


def test_load_failure_does_not_rollback_loaded_files(tmp_path, core):
    """部分成功不回滚：``main`` 已经加载，失败的 ``broken`` 不影响它。"""
    reg(ConfigFile.INI, "nope.ini", name="broken", order=99)
    c = make_config(tmp_path, order=1)

    with pytest.raises(ConfigError):
        c.load()

    assert _FILES["main"].loaded is True
    assert core().debug is True


# ==================== unload() ====================

def test_unload_removes_values_and_resets_flag(tmp_path, core):
    c = make_config(tmp_path)
    c.load()
    assert core().debug is True

    c.unload("main")

    assert _FILES["main"].loaded is False
    assert _FILES["main"].resolved_path is None
    assert ("core", "debug") not in c.resolver
    with pytest.raises(ConfigMissingError):
        _ = core().debug


def test_unload_twice_is_noop(tmp_path):
    c = make_config(tmp_path)
    c.load()
    c.unload("main")
    c.unload("main")  # 幂等，不抛
    assert _FILES["main"].loaded is False


def test_unload_unknown_file_raises_keyerror(tmp_path):
    c = make_config(tmp_path)
    with pytest.raises(KeyError):
        c.unload("nope")


def test_unload_then_load_again(tmp_path, core):
    c = make_config(tmp_path)
    c.load()
    write_ini(tmp_path / "config.ini", "[core]\nname = reloaded\n")

    c.unload("main")
    report = c.load()

    assert report.loaded_files == ("main",)
    assert core().name == "reloaded"


# ==================== flush() ====================

def test_flush_without_dirty_returns_empty(tmp_path):
    c = make_config(tmp_path)
    c.load()
    assert c.flush() == []
    assert c.flush(file="main") == []


def test_flush_unknown_file_raises_keyerror(tmp_path):
    c = make_config(tmp_path)
    c.load()
    with pytest.raises(KeyError):
        c.flush(file="nope")


def test_flush_file_not_loaded_raises_runtime_error(tmp_path):
    c = make_config(tmp_path)
    with pytest.raises(RuntimeError, match="not loaded"):
        c.flush(file="main")


def test_flush_file_and_only_are_mutually_exclusive(tmp_path):
    c = make_config(tmp_path)
    c.load()
    with pytest.raises(ValueError, match="mutually exclusive"):
        c.flush(file="main", only=[("core", "debug")])


def test_flush_writes_class_view_value_to_origin(tmp_path, core):
    c = make_config(tmp_path, autosave="off")
    add_machine_file(tmp_path, autosave="off")
    c.load()
    assert core().name == "machine-wins"

    core().name = "edited"           # origin 是 machine.ini
    written = c.flush()

    assert written == [(tmp_path / "machine.ini").resolve()]
    assert "edited" in (tmp_path / "machine.ini").read_text(encoding="utf-8")
    assert "hello" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_flush_file_limits_to_that_file(tmp_path):
    c = make_config(tmp_path, autosave="off")
    add_machine_file(tmp_path, autosave="off")
    c.load()

    c.tree.main.core.port = 2222       # 归属 main
    c.tree.machine.core.name = "from-machine"  # 归属 machine

    written = c.flush(file="main")

    assert written == [(tmp_path / "config.ini").resolve()]
    main_text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    machine_text = (tmp_path / "machine.ini").read_text(encoding="utf-8")
    assert "2222" in main_text
    assert "from-machine" not in machine_text


def test_flush_new_value_of_declared_field_goes_to_declaring_file(tmp_path, core):
    """只有默认值（文件里没有）的字段被赋值 → 写回声明它的文件。"""
    c = make_config(tmp_path, autosave="off")
    c.load()

    core().extra = "changed"
    written = c.flush()

    assert written == [(tmp_path / "config.ini").resolve()]
    assert "changed" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_flush_unknown_node_raises(tmp_path):
    c = make_config(tmp_path)
    c.load()
    with pytest.raises(ConfigError, match="unknown nodes"):
        c.flush(only=[("core", "nope")])


def test_flush_preserves_comments_and_order(tmp_path, core):
    content = (
        "# top comment\n[core]\n# inline\ndebug = true\nname = hello\n"
        "port = 1234\n"
    )
    c = make_config(tmp_path, autosave="off", content=content)
    c.load()

    core().port = 4321
    c.flush()

    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert "# top comment" in text
    assert "# inline" in text
    assert "port = 4321" in text


def test_save_alias_matches_flush_file(tmp_path, core):
    c = make_config(tmp_path, autosave="off")
    c.load()
    core().port = 7777

    assert c.tree.main.save() == [(tmp_path / "config.ini").resolve()]
    assert c.flush(file="main") == []  # 已经写干净了


def test_tree_save_on_unloaded_file_raises(tmp_path):
    c = make_config(tmp_path)
    with pytest.raises(RuntimeError, match="not loaded"):
        c.tree.main.save()


# ==================== 文件级 autosave 策略 ====================

def test_autosave_off_requires_explicit_flush(tmp_path, core):
    c = make_config(tmp_path, autosave="off")
    c.load()
    core().port = 1111

    assert "1111" not in (tmp_path / "config.ini").read_text(encoding="utf-8")
    c.flush()
    assert "1111" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_autosave_now_writes_immediately(tmp_path, core):
    c = make_config(tmp_path, autosave="now")
    c.load()

    core().port = 2222
    assert "2222" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_autosave_debounce_writes_after_interval(tmp_path, core):
    c = make_config(tmp_path, autosave="debounce", debounce=0.01)
    c.load()

    core().port = 3333
    assert "3333" not in (tmp_path / "config.ini").read_text(encoding="utf-8")

    deadline = time.time() + 3
    while time.time() < deadline:
        if "3333" in (tmp_path / "config.ini").read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    assert "3333" in (tmp_path / "config.ini").read_text(encoding="utf-8")

    c.close()


def test_autosave_atexit_waits_for_close(tmp_path, core):
    c = make_config(tmp_path, autosave="atexit")
    c.load()

    core().port = 4444
    assert "4444" not in (tmp_path / "config.ini").read_text(encoding="utf-8")

    c.close()
    assert "4444" in (tmp_path / "config.ini").read_text(encoding="utf-8")


# ==================== close() ====================

def test_close_is_idempotent(tmp_path):
    c = make_config(tmp_path, autosave="off")
    c.load()
    c.close()
    c.close()


def test_close_flushes_dirty(tmp_path, core):
    c = make_config(tmp_path, autosave="off")
    c.load()
    core().port = 5555
    c.close()

    assert "5555" in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_close_keeps_instance(tmp_path):
    c = make_config(tmp_path, autosave="off")
    c.close()

    again = Configue(tmp_path)
    assert again is c
    assert again._closed is True


def test_close_cancels_debounce_timer(tmp_path, core):
    c = make_config(tmp_path, autosave="debounce", debounce=10)
    c.load()
    core().port = 6666
    assert c._debounce_timers

    c.close()
    assert c._debounce_timers == {}
