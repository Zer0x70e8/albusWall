#
"""``configue.schema``：两遍写回算法（写盘 / dry-run / 键形式 / 失败语义）。

不变量在这里逐条落实：``dry_run`` 不写内存不写盘；先全量校验后统一写；
值没变不写盘；只接受已声明节点；只写已加载文件；写成功后不再 dirty。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg
from albuswall.configue.errors import ConfigError
from albuswall.configue.schema import (
    _MANUAL_SCHEMA,
    invalidate_schema,
    register_setting_item,
    save_group,
    save_values,
    setting_item,
)


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
def demo(tmp_path: Path):
    """``[demo]`` / ``[other]`` 两个分组共用一个 INI 文件。

    ``other.batch_size`` 与 ``demo.batch_size`` 同名，用来验证「裸叶子名在
    本次范围内不唯一时不命中」。
    """
    path = tmp_path / "a.ini"
    path.write_text(
        "[demo]\nbatch_size = 32\nenabled = false\nratio = 1.5\n"
        "[other]\nbatch_size = 8\n",
        encoding="utf-8",
    )

    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        batch_size: int = 32
        enabled: bool = False
        ratio: float = 1.5

    @configclass("other", "a")
    class Other:
        batch_size: int = 8

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 写盘
# ══════════════════════════════════════════════════════════════

def test_save_values_writes_and_cleans(demo):
    path = demo.base_dir / "a.ini"
    result = save_values({"demo.batch_size": 64}, config=demo)

    assert result.applied == ("demo.batch_size",)
    assert result.flushed == ("demo.batch_size",)
    assert result.dry_run is False
    assert path in result.written
    assert "demo.batch_size" in result.by_file[path]
    assert "batch_size = 64" in _read(path)
    assert demo.dirty_nodes() == frozenset()
    assert demo.resolver.get_typed("demo", "batch_size") == 64


def test_dry_run_touches_nothing(demo):
    path = demo.base_dir / "a.ini"
    before_text = _read(path)
    before_mtime = path.stat().st_mtime_ns

    result = save_values(
        {"demo.batch_size": 64}, config=demo, dry_run=True,
    )

    assert result.dry_run is True
    assert result.written == ()
    assert result.flushed == ()
    assert result.applied == ()
    assert "demo.batch_size" in result.by_file[path]

    change = result.changes[0]
    assert change["key"] == "demo.batch_size"
    assert change["path"] == "[demo]batch_size"
    assert change["old"] == 32
    assert change["new"] == 64
    assert change["text"] == "64"
    assert change["file"] == str(path)
    assert change["changed"] is True

    # 内存与磁盘都没动，也没有 mark_clean 之后的错觉
    assert demo.resolver.get_typed("demo", "batch_size") == 32
    assert _read(path) == before_text
    assert path.stat().st_mtime_ns == before_mtime
    assert demo.dirty_nodes() == frozenset()


def test_unchanged_value_is_skipped(demo):
    path = demo.base_dir / "a.ini"
    first = save_values({"demo.batch_size": 64}, config=demo)
    assert first.applied == ("demo.batch_size",)

    second = save_values({"demo.batch_size": 64}, config=demo)
    assert second.applied == ()
    assert second.flushed == ()
    assert second.written == ()
    assert second.changes[0]["changed"] is False
    assert "batch_size = 64" in _read(path)


# ══════════════════════════════════════════════════════════════
# 键形式
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "key",
    [
        "demo.batch_size",
        ("demo", "batch_size"),
        "[demo]batch_size",
    ],
)
def test_key_forms(demo, key):
    result = save_values({key: 64}, config=demo)
    assert result.applied == ("demo.batch_size",)
    assert demo.resolver.get_typed("demo", "batch_size") == 64


def test_bare_name_resolves_when_unique(demo):
    result = save_values({"enabled": True}, config=demo)
    assert result.applied == ("demo.enabled",)
    assert demo.resolver.get_typed("demo", "enabled") is True


def test_bare_name_ambiguous_is_not_matched(demo):
    """两个组都有 ``batch_size``：裸名不命中（报歧义），不会越权写到别组。"""
    path = demo.base_dir / "a.ini"
    before = _read(path)

    with pytest.raises(ValueError, match="ambiguous"):
        save_values({"batch_size": 64}, config=demo)

    assert _read(path) == before
    assert demo.dirty_nodes() == frozenset()
    assert demo.resolver.get_typed("demo", "batch_size") == 32
    assert demo.resolver.get_typed("other", "batch_size") == 8


# ══════════════════════════════════════════════════════════════
# 类型转换（与读配置同源）
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "node,raw,expected",
    [
        (("demo", "batch_size"), "64", 64),
        (("demo", "enabled"), "true", True),
        (("demo", "ratio"), "2.5", 2.5),
    ],
)
def test_string_coercion(demo, node, raw, expected):
    result = save_values({node: raw}, config=demo)
    assert result.applied, "值应该被写入"
    assert demo.resolver.get_typed(*node) == expected


def test_bad_coercion_raises_value_error(demo):
    with pytest.raises(ValueError):
        save_values({"demo.batch_size": "abc"}, config=demo)
    assert demo.resolver.get_typed("demo", "batch_size") == 32
    assert demo.dirty_nodes() == frozenset()


# ══════════════════════════════════════════════════════════════
# 拒绝：未声明 / 无归属
# ══════════════════════════════════════════════════════════════

def test_undeclared_key_is_rejected(demo):
    path = demo.base_dir / "a.ini"
    before = _read(path)
    with pytest.raises(ValueError):
        save_values({"demo.ghost_key": 1}, config=demo)
    # 不允许制造 ghost key：文件与内存都没动
    assert "ghost_key" not in _read(path)
    assert (("demo", "ghost_key") in demo.resolver) is False
    assert before == _read(path)


def test_no_owning_file_raises(tmp_path: Path):
    reg(ConfigFile.INI, "missing.ini", name="missing", order=10)

    @configclass("ghost", "missing")
    class Ghost:
        level: int = 1

    cfg = Configue(base_dir=tmp_path)      # 不 load：没有已解析路径

    with pytest.raises(ValueError):
        save_values({"ghost.level": 5}, config=cfg)

    assert cfg.dirty_nodes() == frozenset()
    assert not (tmp_path / "missing.ini").exists()
    assert ("ghost", "level") not in cfg.resolver


# ══════════════════════════════════════════════════════════════
# 写盘失败：strict 语义
# ══════════════════════════════════════════════════════════════

def _make_unwritable(directory: Path) -> None:
    """让目录不可写：原子写要在同目录建临时文件，权限一收就必然失败。"""
    os.chmod(directory, 0o555)


def _make_writable(directory: Path) -> None:
    os.chmod(directory, 0o755)


def test_write_failure_strict_raises(demo):
    _make_unwritable(demo.base_dir)
    try:
        with pytest.raises(ConfigError):
            save_values({"demo.batch_size": 64}, config=demo, strict=True)
    finally:
        _make_writable(demo.base_dir)


def test_write_failure_lenient_keeps_dirty(demo):
    _make_unwritable(demo.base_dir)
    try:
        result = save_values({"demo.batch_size": 64}, config=demo)
    finally:
        _make_writable(demo.base_dir)

    assert result.applied == ("demo.batch_size",)
    assert result.flushed == ()
    assert result.written == ()
    # 写盘失败保留 dirty，等下次 flush 再试
    assert ("demo", "batch_size") in demo.dirty_nodes()
    assert demo.last_flush_error is not None


# ══════════════════════════════════════════════════════════════
# save_group
# ══════════════════════════════════════════════════════════════

def test_save_group_only_touches_that_group(demo):
    result = save_group(
        "other", {"batch_size": 9}, config=demo,
    )
    assert result.applied == ("other.batch_size",)
    assert demo.resolver.get_typed("other", "batch_size") == 9
    assert demo.resolver.get_typed("demo", "batch_size") == 32   # 没被连坐


def test_save_group_unknown_raises_keyerror(demo):
    with pytest.raises(KeyError):
        save_group("nope", {"batch_size": 9}, config=demo)


def test_save_group_dry_run(demo):
    result = save_group("demo", {"batch_size": 64}, config=demo, dry_run=True)
    assert result.dry_run is True
    assert result.written == ()
    assert demo.resolver.get_typed("demo", "batch_size") == 32


# ══════════════════════════════════════════════════════════════
# 手写覆盖参与写回
# ══════════════════════════════════════════════════════════════

def test_manual_override_does_not_change_written_file(demo):
    register_setting_item("demo", setting_item("batch_size", label="批量"))
    result = save_values({"demo.batch_size": 64}, config=demo)
    assert result.applied == ("demo.batch_size",)
    assert "batch_size = 64" in _read(demo.base_dir / "a.ini")
