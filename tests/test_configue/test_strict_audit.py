#
"""两个「审计开关」：``load(strict_undeclared=True)`` 与 ``strict_tree_writes``。

设计前提（改动时别丢）：

- 两者**默认都关**，零行为变化；打开后是「让问题显形」，不是「改语义」。
- ``strict_undeclared`` 只产出 ``LoadReport.warnings``，**不影响成败**，
  也**不阻止 key 生效**——强制隔离会误伤 ``[shortcuts]`` 这类运行期才知道
  key 的合法配置。
- 检查必须在**加载钩子之后**：hook 补过类型的 key 算「已登记」。
- ``strict_tree_writes`` 只拦**写入**（拼错 key 静默落盘是最危险的场景），
  读侧不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import (
    ConfigFile,
    Configue,
    configclass,
    reg,
    register_load_hook,
)

from .conftest import write_ini

UNDECLARED_WARNING = "undeclared key [{section}]{key} "


def _warnings(report) -> list[str]:
    return list(report.warnings)


# ==================== A. load(strict_undeclared=True) ====================

def test_undeclared_warning_is_off_by_default(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\ntypo = 1\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    c = Configue(tmp_path)
    report = c.load()

    assert report.warnings == ()
    assert report.ok is True


def test_undeclared_warning_reports_ghost_key(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\ntypo = 1\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    c = Configue(tmp_path)
    report = c.load(strict_undeclared=True)

    assert _warnings(report) == [
        "undeclared key [core]typo (no @configclass, no load hook)"
    ]
    # 审计只报不改：幽灵 key 照常生效（无类型声明 → 原始字符串）
    assert report.ok is True
    assert c.tree.main.core.typo == "1"


def test_declared_and_default_only_keys_are_not_reported(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False
        extra: str = "dflt"          # 文件里没有，只有默认值

    c = Configue(tmp_path)
    report = c.load(strict_undeclared=True)

    assert report.warnings == ()


def test_hook_registered_keys_are_not_reported(tmp_path: Path):
    """``[shortcuts]`` 的 key 由 hook 补类型，不能被误报成幽灵配置。"""
    write_ini(
        tmp_path / "config.ini",
        "[shortcuts]\nopen = Ctrl+O\nquit = Ctrl+Q\n[core]\ntypo = 1\n",
    )
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    def hook(resolver):
        for key in resolver.keys_of("shortcuts"):
            resolver.register_type("shortcuts", key, list[str])

    register_load_hook(hook)
    c = Configue(tmp_path)
    report = c.load(strict_undeclared=True)

    assert _warnings(report) == [
        "undeclared key [core]typo (no @configclass, no load hook)"
    ]
    assert c.tree.main.shortcuts.open == ["Ctrl+O"]


def test_undeclared_warnings_are_sorted_and_one_per_node(tmp_path: Path):
    write_ini(
        tmp_path / "config.ini",
        "[b]\nz = 1\na = 2\n[a]\nb = 3\n",
    )
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    report = c.load(strict_undeclared=True)

    assert _warnings(report) == [
        "undeclared key [a]b (no @configclass, no load hook)",
        "undeclared key [b]a (no @configclass, no load hook)",
        "undeclared key [b]z (no @configclass, no load hook)",
    ]


def test_undeclared_warnings_do_not_block_errors_from_raising(tmp_path: Path):
    """warnings 不影响 ``partial=False`` 的抛错判定（只有 errors 才抛）。"""
    write_ini(tmp_path / "config.ini", "[core]\ntypo = 1\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    report = c.load(strict_undeclared=True)      # 不抛，只有 warning

    assert report.ok is True
    assert _warnings(report)


# ==================== B. strict_tree_writes ====================

@pytest.fixture
def tree_env(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[core]\ndebug = false\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    c = Configue(tmp_path)
    c.load()
    return c, write_ini


def test_strict_tree_writes_defaults_to_false(tree_env):
    c, _ = tree_env

    assert c.strict_tree_writes is False
    c.tree.main.core.new_key = 1                 # 默认照常写
    assert c.tree.main.core.new_key == 1


def test_strict_tree_writes_rejects_undeclared_key(tree_env):
    c, _ = tree_env
    c.strict_tree_writes = True

    with pytest.raises(AttributeError, match=r"\[core\]debgu.*write refused"):
        c.tree.main.core.debgu = True

    # 拒绝写入 = 值没进 Resolver，也不会在 save() 时多出一行
    assert "debgu" not in c.tree.main.core.keys()
    c.tree.main.save()
    assert "debgu" not in (c.base_dir / "config.ini").read_text(encoding="utf-8")


def test_strict_tree_writes_allows_declared_key(tree_env):
    c, _ = tree_env
    c.strict_tree_writes = True

    c.tree.main.core.debug = True

    assert c.tree.main.core.debug is True


def test_strict_tree_writes_rejects_via_setitem(tree_env):
    c, _ = tree_env
    c.strict_tree_writes = True

    with pytest.raises(AttributeError, match="write refused"):
        c.tree.main.core["debgu"] = True


def test_strict_tree_writes_allows_hook_registered_key(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[shortcuts]\nopen = Ctrl+O\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    def hook(resolver):
        for key in resolver.keys_of("shortcuts"):
            resolver.register_type("shortcuts", key, list[str])

    register_load_hook(hook)
    c = Configue(tmp_path)
    c.load()
    c.strict_tree_writes = True

    c.tree.main.shortcuts.open = ["Ctrl+P"]      # hook 登记过 → 可写
    assert c.tree.main.shortcuts.open == ["Ctrl+P"]

    with pytest.raises(AttributeError, match="write refused"):
        c.tree.main.shortcuts.typo = ["Ctrl+T"]


def test_strict_tree_writes_still_reports_unloaded_file_first(tmp_path: Path):
    """未加载是更根本的问题：应先报「not loaded」，而不是「未声明」。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = false\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    @configclass("core", "main")
    class Core:
        debug: bool = False

    c.strict_tree_writes = True

    # 文件级守卫先触发（``_FileView.__getattr__``），错误信息里是 not loaded
    with pytest.raises(AttributeError, match="not loaded"):
        c.tree.main.core.debug = True


def test_strict_tree_writes_setter_coerces_to_bool(tree_env):
    c, _ = tree_env

    c.strict_tree_writes = 1
    assert c.strict_tree_writes is True
    c.strict_tree_writes = None
    assert c.strict_tree_writes is False
