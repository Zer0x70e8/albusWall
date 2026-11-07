#
"""``setting.*`` 命令：注册 / 分派 / 错误翻译 / 无 Qt-仓储依赖。

用**真** ``Configue``（``tmp_path`` + ``reg`` + ``load``）+ 装进容器的
``config`` 键，走 ``CommandService``（≈ 一条 IPC 请求的真实路径）。

注意：``python -m albuswall setting ...`` 会启动 GUI（``entrypoint`` 只认
白名单子命令），所以命令层的用例一律直接走注册表 / ``CommandService``。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from pathlib import Path

import pytest

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest, UnknownCommand
from albuswall.configue import ConfigFile, Configue, configclass, field, reg
from albuswall.configue.schema import _MANUAL_SCHEMA, invalidate_schema
from albuswall.core import Container
from albuswall.service import CommandService
from albuswall.service.command.handlers import setup as setup_handlers


@pytest.fixture(autouse=True)
def _isolated_manual_schema():
    _MANUAL_SCHEMA.clear()
    invalidate_schema()
    try:
        yield
    finally:
        _MANUAL_SCHEMA.clear()
        invalidate_schema()


@pytest.fixture
def service(tmp_path: Path, fake_container: Container):
    """真配置 + 装好 handler 的 ``CommandService``。

    ``fake_container`` 来自 ``tests/conftest.py``（只装 service stub）；
    这里额外塞一个**真** ``Configue``——handler 懒取的就是它。
    """
    path = tmp_path / "a.ini"
    path.write_text("[demo]\nbatch_size = 32\n", encoding="utf-8")

    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        batch_size: int = 32

    cfg = Configue(base_dir=tmp_path)
    cfg.load()

    fake_container.set("config", cfg)

    registry = CommandRegistry()
    setup_handlers(registry, fake_container)
    return CommandService(registry), cfg, path


# ══════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════

def test_commands_are_registered(service):
    svc, _cfg, _path = service
    names = set(svc.registry.names(
        include_hidden=True, subject=frozenset({"admin"}),
    ))
    assert {"setting", "setting.schema", "setting.set", "setting.save"} <= names


def test_namespace_usage_and_unknown_subcommand(service):
    svc, _cfg, _path = service

    usage = svc.execute("setting")
    assert "用法: albuswall setting <subcommand>" in usage
    for sub in ("schema", "set", "save"):
        assert sub in usage

    with pytest.raises(UnknownCommand):
        svc.execute("setting", ["nope"])


def test_help_is_readable(service):
    svc, _cfg, _path = service
    detail = svc.execute("help", ["setting.set"])
    for marker in ("--values", "--group", "--dry-run", "--strict"):
        assert marker in detail


# ══════════════════════════════════════════════════════════════
# setting.schema
# ══════════════════════════════════════════════════════════════

def test_schema_default_is_text(service):
    svc, _cfg, _path = service
    out = svc.execute("setting.schema")
    assert isinstance(out, str)
    assert "Settings schema:" in out


def test_schema_views(service):
    svc, _cfg, _path = service

    flat = svc.execute("setting", ["schema"], {"view": "flat"})
    assert isinstance(flat, list)
    assert flat[0]["key"] == "demo.batch_size"

    tree = svc.execute("setting.schema", kwargs={"view": "tree"})
    assert set(tree) == {
        "schema_version", "groups", "items", "by_file", "unbound", "summary",
        "source", "uncovered",
    }

    by_file = svc.execute("setting.schema", kwargs={"view": "by_file"})
    assert "files" in by_file and "unbound" in by_file
    assert "uncovered" in by_file


def test_schema_undeclared_lists_unknown_keys(service):
    """``--undeclared``：文件里有、设置树枚举不到的 key 必须列出来。"""
    svc, cfg, path = service
    extra = path.parent / "b.ini"
    extra.write_text("[misc]\nsecret = 42\n", encoding="utf-8")
    reg(ConfigFile.INI, "b.ini", name="b", order=50)
    cfg.load()          # load() 只加载「尚未加载」的文件，正好把 b.ini 读进来

    text = svc.execute("setting.schema", kwargs={"undeclared": True})
    assert isinstance(text, str)
    assert "[misc]secret" in text

    rows = svc.execute(
        "setting.schema", kwargs={"undeclared": True, "view": "flat"},
    )
    assert isinstance(rows, list)
    assert [row["path"] for row in rows] == ["[misc]secret"]
    assert rows[0]["typed"] is False


def test_schema_unknown_view_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest) as excinfo:
        svc.execute("setting.schema", kwargs={"view": "bogus"})
    assert "bogus" in str(excinfo.value)


def test_schema_works_without_config_in_container(fake_container):
    """容器里没有 ``config`` 时 schema 仍然可用（降级 dump）。"""
    registry = CommandRegistry()
    setup_handlers(registry, fake_container)
    svc = CommandService(registry)
    out = svc.execute("setting.schema", kwargs={"view": "flat"})
    assert isinstance(out, list)


# ══════════════════════════════════════════════════════════════
# setting.set
# ══════════════════════════════════════════════════════════════

def test_set_dry_run_does_not_write(service):
    svc, cfg, path = service
    before = path.read_text(encoding="utf-8")

    result = svc.execute(
        "setting.set", ["demo.batch_size=64"], {"dry-run": True},
    )

    assert result["dry_run"] is True
    assert result["written"] == []
    assert result["applied"] == []
    assert result["ok"] is True
    assert result["pending"] == []
    assert result["changes"][0]["new"] == 64
    assert cfg.resolver.get_typed("demo", "batch_size") == 32
    assert path.read_text(encoding="utf-8") == before


def test_set_writes_and_reports(service):
    svc, cfg, path = service

    result = svc.execute("setting.set", ["demo.batch_size=64"])

    assert result["dry_run"] is False
    assert result["applied"] == ["demo.batch_size"]
    assert result["flushed"] == ["demo.batch_size"]
    assert result["written"] == [str(path)]
    assert result["pending"] == []
    assert result["ok"] is True
    assert result["error"] is None
    assert "batch_size = 64" in path.read_text(encoding="utf-8")
    assert cfg.dirty_nodes() == frozenset()


def test_set_reports_failure_for_already_dirty_node(service, monkeypatch):
    """「上次就脏、这次又写失败」必须报 ``ok=False``（差集法会报成功）。"""
    svc, cfg, path = service

    # 先留一个上次写盘失败的节点（写内存成功、flush 失败 → 仍然 dirty）
    monkeypatch.setattr(Configue, "flush", lambda *a, **k: [])
    cfg.resolver.set("demo", "batch_size", 99, persist=True)
    assert ("demo", "batch_size") in cfg.dirty_nodes()

    result = svc.execute("setting.set", ["demo.batch_size=64"])

    assert result["ok"] is False
    assert result["pending"] == ["[demo]batch_size"]
    assert "batch_size = 64" not in path.read_text(encoding="utf-8")


def test_set_via_values_option_and_group(service):
    svc, cfg, path = service

    svc.execute(
        "setting.set", [], {"values": '{"demo.batch_size": 16}'},
    )
    assert cfg.resolver.get_typed("demo", "batch_size") == 16

    svc.execute(
        "setting.set", ["batch_size=8"], {"group": "demo"},
    )
    assert cfg.resolver.get_typed("demo", "batch_size") == 8
    assert "batch_size = 8" in path.read_text(encoding="utf-8")


def test_set_requires_values(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute("setting.set", [])
    with pytest.raises(BadRequest):
        svc.execute("setting.set", [], {"values": "{}"})


def test_set_duplicate_key_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute(
            "setting.set",
            ["demo.batch_size=1"],
            {"values": {"demo.batch_size": 2}},
        )
    with pytest.raises(BadRequest):
        svc.execute(
            "setting.set", ["demo.batch_size=1", "demo.batch_size=2"],
        )


def test_set_unknown_key_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute("setting.set", ["demo.nope=1"])


def test_set_bad_value_is_bad_request(service):
    svc, cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute("setting.set", ["demo.batch_size=abc"])
    assert cfg.dirty_nodes() == frozenset()


def test_set_unknown_group_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute(
            "setting.set", ["batch_size=8"], {"group": "nope"},
        )


def test_set_malformed_assignment_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute("setting.set", ["batch_size"])


def test_set_unknown_option_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute(
            "setting.set", ["demo.batch_size=1"], {"nope": True},
        )


# ══════════════════════════════════════════════════════════════
# setting.save
# ══════════════════════════════════════════════════════════════

def test_save_dry_run_when_clean(service):
    svc, _cfg, _path = service
    result = svc.execute("setting.save", kwargs={"dry-run": True})
    assert result["dry_run"] is True
    assert result["ok"] is True
    assert result["pending"] == []
    assert result["written"] == []


def test_save_flushes_dirty_nodes(service):
    svc, cfg, path = service
    cfg.resolver.set("demo", "batch_size", 64, persist=True)
    assert cfg.dirty_nodes() == frozenset({("demo", "batch_size")})

    result = svc.execute("setting.save")
    assert result["ok"] is True
    assert result["written"] == [str(path)]
    assert result["pending"] == []
    assert "batch_size = 64" in path.read_text(encoding="utf-8")


def test_save_with_unknown_file_is_bad_request(service):
    svc, _cfg, _path = service
    with pytest.raises(BadRequest):
        svc.execute("setting.save", kwargs={"file": "nope"})


def test_save_works_without_language_sink(service):
    """改 ``[ui] language`` 时没有 i18n 也不应报错（旁路静默跳过）。"""
    svc, _cfg, _path = service
    result = svc.execute("setting.set", ["demo.batch_size=12"])
    assert result["ok"] is True


# ══════════════════════════════════════════════════════════════
# 契约：无 Qt / 无仓储
# ══════════════════════════════════════════════════════════════

def _handler_source() -> str:
    module = __import__(
        "albuswall.service.command.handlers.setting", fromlist=["x"],
    )
    return pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")


def test_setting_handler_has_no_forbidden_imports():
    """``setting.py`` 不得 import 仓储 / UI（配置层无 Qt、无 repo）。"""
    tree = ast.parse(_handler_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "repositories" not in module, f"import 了 {module}"
            assert not module.startswith("albuswall.ui"), f"import 了 {module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "repositories" not in alias.name, alias.name
                assert not alias.name.startswith("albuswall.ui"), alias.name


def test_setting_handler_setup_does_not_require_config():
    """``setup`` 里不得 ``container.require("config")``（测试容器没有该键）。"""
    tree = ast.parse(_handler_source())
    setup = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "setup"
    )
    own_nodes: list[ast.AST] = []
    for stmt in setup.body:
        # 只看 ``setup`` 自己的语句：嵌套的命令函数体内当然要用 container
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own_nodes.extend(ast.walk(stmt))

    for node in own_nodes:
        is_container_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "container"
        )
        if is_container_call:
            raise AssertionError(
                "setup 里不应该解析容器依赖："
                f"container.{node.func.attr}() 必须在命令函数体内懒取"
            )


# ══════════════════════════════════════════════════════════════
# setting.set：校验失败 → BadRequest（64），不是 CommandFailed（70）
# ══════════════════════════════════════════════════════════════

def _port_range(value: int) -> None:
    if not 1 <= value <= 65535:
        raise ValueError("端口必须在 1-65535 之间")


@pytest.fixture
def checked_service(tmp_path: Path, fake_container: Container):
    """带 ``validators`` 的真配置：``[demo]port = 8000``。"""
    path = tmp_path / "a.ini"
    path.write_text("[demo]\nport = 8000\n", encoding="utf-8")

    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Checked:
        port: int = field(default=8000, validators=[_port_range])

    cfg = Configue(base_dir=tmp_path)
    cfg.load()

    fake_container.set("config", cfg)
    registry = CommandRegistry()
    setup_handlers(registry, fake_container)
    return CommandService(registry), cfg, path


def test_validation_failure_is_bad_request(checked_service):
    """校验失败是**调用方**的错：400，且文案原样透出（含 ``[section]key``）。"""
    svc, cfg, _path = checked_service

    with pytest.raises(BadRequest) as info:
        svc.execute("setting", ["set", "demo.port=99999"])

    exc = info.value
    assert exc.code == "bad_request"
    assert exc.exit_code == 64
    assert "[demo]port" in str(exc)
    assert "端口必须在 1-65535 之间" in str(exc)
    # 不是「命令执行失败」的包装文案
    assert "raised" not in str(exc)
    # 内存与磁盘都没动
    assert cfg.resolver.get_typed("demo", "port") == 8000
    assert cfg.dirty_nodes() == frozenset()


def test_valid_value_still_writes(checked_service):
    svc, cfg, path = checked_service

    out = svc.execute("setting", ["set", "demo.port=8080"])

    assert out["ok"] is True
    assert out["applied"] == ["demo.port"]
    assert cfg.resolver.get_typed("demo", "port") == 8080
    assert "port = 8080" in path.read_text(encoding="utf-8")


def test_coerce_failure_keeps_original_mapping(checked_service):
    """非校验失败（coerce 转不过去）仍走原来的 ``ValueError`` 分支。"""
    svc, cfg, _path = checked_service

    with pytest.raises(BadRequest) as info:
        svc.execute("setting", ["set", "demo.port=abc"])

    assert info.value.code == "bad_request"
    assert "cannot convert" in str(info.value)
    assert cfg.resolver.get_typed("demo", "port") == 8000


def test_validation_failure_via_group_is_bad_request(checked_service):
    svc, cfg, _path = checked_service

    with pytest.raises(BadRequest) as info:
        svc.execute("setting", ["set", "port=0"], {"group": "demo"})

    assert "[demo]port" in str(info.value)
    assert cfg.resolver.get_typed("demo", "port") == 8000


def test_validation_failure_on_save_group_path(checked_service):
    """``save_group`` 分支同样映射成 BadRequest（同一个 except）。"""
    svc, cfg, _path = checked_service

    with pytest.raises(BadRequest) as info:
        svc.execute(
            "setting", ["set"], {"group": "demo", "values": '{"port": -1}'},
        )

    assert "[demo]port" in str(info.value)
    assert cfg.resolver.get_typed("demo", "port") == 8000


def test_setting_save_does_not_validate(checked_service):
    """``setting.save`` 只落盘已有 dirty 节点，不跑校验器（无 400）。"""
    svc, _cfg, _path = checked_service
    out = svc.execute("setting", ["save"])
    assert out["dry_run"] is False
    assert out["ok"] is True
