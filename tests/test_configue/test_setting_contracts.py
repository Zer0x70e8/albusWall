#
"""``configue.schema`` / 设置写回的**契约**回归测试。

这一组用例锁的是「行为与承诺一致」，每个用例对应一条曾经不成立的承诺：

1. 转换后的值必须能被自己读回来（写 ``3.7`` 进 ``int`` 字段 = 下次启动崩）；
2. ``dump`` 说写进哪个文件，``save`` 就必须写进哪个文件（同一决策函数）；
3. ``editable=False`` 是行为（拒绝写入），不是展示标记；
4. ``pending`` 按本次碰过的节点判定，不用 dirty 差集（否则失败报成功）；
5. ``flush=False`` 只写内存（UI 的草稿 + Apply 两段式），不落盘；
6. 序列化失败在第一遍就报，不留到 ``flush`` 静默留 dirty；
7. ``uncovered`` 让「文件里有、树里没有」的节点可见；
8. 手写覆盖：``section/key`` 写错时报错、不共享进程级对象、不改叶子名。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg
from albuswall.configue.schema import (
    _MANUAL_SCHEMA,
    _save_entries,
    build_schema,
    dump_schema,
    dump_schema_dict,
    invalidate_schema,
    register_setting_group,
    register_setting_item,
    save_values,
    setting_group,
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
    """``[demo]`` 一个分组，含 int / bool / str / 可选 int 四类字段。"""
    path = tmp_path / "a.ini"
    path.write_text(
        "[demo]\nport = 1234\nenabled = false\nname = albus\n",
        encoding="utf-8",
    )
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        port: int = 1234
        enabled: bool = False
        name: str = "albus"

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _read(cfg: Configue) -> str:
    return (cfg.base_dir / "a.ini").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 1. 类型回验：写进去的值必须读得回来
# ══════════════════════════════════════════════════════════════

def test_float_into_int_field_is_rejected(demo):
    """``int`` 字段收到 ``3.7`` → 报错，而不是写进 ``3.7`` 等下次启动崩。"""
    with pytest.raises(ValueError) as excinfo:
        save_values({"demo.port": 3.7}, config=demo)
    assert "port" in str(excinfo.value) or "3.7" in str(excinfo.value)
    # 磁盘与内存都没动
    assert "port = 1234" in _read(demo)
    assert demo.resolver.get_typed("demo", "port") == 1234


def test_dry_run_reports_type_mismatch_as_error(demo):
    """dry-run 也不放过类型不符：它只收集，不写。"""
    with pytest.raises(ValueError):
        save_values({"demo.port": 3.7}, config=demo, dry_run=True)


def test_typed_value_that_matches_is_accepted(demo):
    result = save_values({"demo.port": 4321}, config=demo)
    assert result.applied == ("demo.port",)
    assert demo.resolver.get_typed("demo", "port") == 4321
    # 读回来还是 int（INI 里的文本形态 ``4321`` 能转回去）
    assert "port = 4321" in _read(demo)


def test_str_field_rejects_int(demo):
    """``str`` 字段收到 JSON 里的 ``3`` 也要拒（否则读回来是 ``"3"``≠``3``）。"""
    with pytest.raises(ValueError):
        save_values({"demo.name": 3}, config=demo)


def test_bool_accepts_int_writing(demo):
    """``bool`` 收到 ``1`` / ``0`` 仍然归一（与运行期赋值一致）。"""
    save_values({"demo.enabled": 1}, config=demo)
    assert demo.resolver.get_typed("demo", "enabled") is True
    assert "enabled = true" in _read(demo)


# ══════════════════════════════════════════════════════════════
# 2. dump 与 save 的落点必须一致
# ══════════════════════════════════════════════════════════════

def test_dump_and_save_agree_on_target_file(demo, tmp_path: Path):
    """显式 ``file=`` 不能盖掉 ``target_file_of``：dump 与 save 同一个落点。"""
    other = tmp_path / "other.ini"
    register_setting_item(
        "demo", setting_item("port", "demo", "port", file=other),
    )

    item = {
        entry["path"]: entry for entry in dump_schema(view="flat")["items"]
    }["[demo]port"]
    assert item["target_file"] == str(demo.base_dir / "a.ini")
    assert item["target_source"] == "resolver"
    assert item["explicit_file"] == str(other)

    result = save_values({"demo.port": 999}, config=demo, dry_run=True)
    assert [change["file"] for change in result.changes] == [
        str(demo.base_dir / "a.ini"),
    ]


def test_explicit_file_is_used_when_resolver_cannot_decide(demo):
    """推不出归属文件时才退回显式 ``file``（``target_source="explicit"``）。"""
    orphan = setting_item("ghost", "ghost", "ghost")
    orphan.file = demo.base_dir / "spare.ini"

    class _Grab(dict):
        pass

    from albuswall.configue.schema import _resolve_item

    group = setting_group("ghost").add(orphan)
    entry = _resolve_item(group, orphan, None, demo)
    assert entry["target_file"] == str(demo.base_dir / "spare.ini")
    assert entry["target_source"] == "explicit"


# ══════════════════════════════════════════════════════════════
# 3. editable=False 是行为
# ══════════════════════════════════════════════════════════════

def test_fixed_item_rejects_writes(demo):
    register_setting_item(
        "demo", setting_item("port", "demo", "port", editable=False),
    )
    with pytest.raises(ValueError) as excinfo:
        save_values({"demo.port": 999}, config=demo)
    assert "fixed" in str(excinfo.value)
    assert "port = 1234" in _read(demo)


def test_fixed_item_does_not_appear_in_dry_run_changes(demo):
    register_setting_item(
        "demo", setting_item("port", "demo", "port", editable=False),
    )
    result = save_values(
        {"demo.port": 999}, config=demo, dry_run=True, collect_errors=True,
    )
    assert result.changes == ()
    assert [err["key"] for err in result.validation_errors] == ["demo.port"]
    assert "fixed" in result.validation_errors[0]["message"]


# ══════════════════════════════════════════════════════════════
# 4. pending 不看 dirty 差集
# ══════════════════════════════════════════════════════════════

def test_already_dirty_node_that_fails_again_is_reported(demo, monkeypatch):
    """「上次就脏、这次又写失败」必须报 ``ok=False``（差集法会报成功）。"""
    from albuswall.ui.window import settings as ui_settings

    # 先制造一个脏节点：写内存但把 flush 换成 no-op（模拟写盘失败）
    monkeypatch.setattr(Configue, "flush", lambda *a, **k: [])
    assert demo.resolver.set("demo", "enabled", True, persist=True) is None \
        or True
    assert ("demo", "enabled") in demo.dirty_nodes()

    report = ui_settings.update_settings({"demo.enabled": False}, config=demo)

    assert report["ok"] is False
    assert report["pending"] == ["[demo]enabled"]


# ══════════════════════════════════════════════════════════════
# 5. flush=False：只写内存
# ══════════════════════════════════════════════════════════════

def test_flush_false_writes_memory_only(demo):
    result = save_values({"demo.port": 777}, config=demo, flush=False)

    assert result.deferred is True
    assert result.written == ()
    assert result.flushed == ()
    assert result.applied == ("demo.port",)
    assert demo.resolver.get_typed("demo", "port") == 777
    assert ("demo", "port") in demo.dirty_nodes()
    assert "port = 777" not in _read(demo)

    # 调用方自己决定什么时候落盘
    demo.flush()
    assert "port = 777" in _read(demo)


# ══════════════════════════════════════════════════════════════
# 6. 序列化失败在第一遍就报
# ══════════════════════════════════════════════════════════════

class _Boom:
    """自定义类型：读得进来，写不出去（序列化必抛）。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Boom) and other.text == self.text

    def __repr__(self) -> str:  # pragma: no cover - 调试
        return f"_Boom({self.text!r})"


def test_unserializable_value_fails_before_any_write(tmp_path: Path):
    path = tmp_path / "b.ini"
    path.write_text("[boom]\nthing = one\n", encoding="utf-8")
    reg(ConfigFile.INI, "b.ini", name="b", order=10)

    from albuswall.configue import register_converter

    def _reader(text: str) -> _Boom:
        return _Boom(text)

    def _writer(value: _Boom) -> str:
        raise RuntimeError("no way to store this")

    register_converter(_Boom, _reader, writer=_writer)

    @configclass("boom", "b")
    class Boom:
        thing: _Boom = _Boom("one")

    cfg = Configue(base_dir=tmp_path)
    cfg.load()

    with pytest.raises(Exception) as excinfo:
        save_values({"boom.thing": _Boom("two")}, config=cfg)

    # 第一遍就失败：内存与磁盘都没动
    assert cfg.resolver.get_typed("boom", "thing") == _Boom("one")
    assert "thing = one" in path.read_text(encoding="utf-8")
    assert "no way to store this" in str(excinfo.value)


# ══════════════════════════════════════════════════════════════
# 7. uncovered：可见性下限
# ══════════════════════════════════════════════════════════════

def test_uncovered_lists_nodes_absent_from_the_tree(tmp_path: Path):
    """文件里手写、没人声明的 key 必须出现在 ``uncovered`` 里。"""
    path = tmp_path / "a.ini"
    path.write_text(
        "[demo]\nport = 1234\n\n[misc]\nsecret = 42\n",
        encoding="utf-8",
    )
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        port: int = 1234

    cfg = Configue(base_dir=tmp_path)
    cfg.load()

    dump = dump_schema_dict(cfg)
    paths = [entry["path"] for entry in dump["uncovered"]]
    assert "[misc]secret" in paths

    entry = next(e for e in dump["uncovered"] if e["path"] == "[misc]secret")
    assert entry["typed"] is False          # 没人声明、也没有 load hook
    assert entry["value_repr"] == repr("42")  # INI 的原始文本形态
    assert dump["summary"]["uncovered_count"] == len(dump["uncovered"])

    # 未声明节点不出现在 items / by_file 里（它们不属于设置树）
    assert "[misc]secret" not in {item["path"] for item in dump["items"]}
    assert all(
        "[misc]secret" not in keys for keys in dump["by_file"].values()
    )


def test_declared_nodes_are_not_uncovered(demo):
    assert dump_schema_dict(demo)["uncovered"] == []


def test_source_counts_declared_items(demo):
    dump = dump_schema_dict(demo)
    assert dump["source"]["declared"] == len(dump["items"])
    assert dump["source"]["manual"] == 0
    assert all(item["source"] == "declared" for item in dump["items"])


# ══════════════════════════════════════════════════════════════
# 8. 手写覆盖的边界
# ══════════════════════════════════════════════════════════════

def test_manual_item_with_wrong_node_does_not_hijack_a_leaf(demo, tmp_path):
    """``section/key`` 写错时报错，而不是静默改掉同名叶子。"""
    register_setting_item(
        "demo",
        setting_item("port", "wrong_section", "port", label="端口"),
    )
    with pytest.raises(ValueError) as excinfo:
        build_schema(demo)
    assert "wrong_section" in str(excinfo.value)


def test_manual_item_does_not_rename_the_leaf(demo):
    register_setting_item(
        "demo", setting_item("端口", "demo", "port", label="端口号"),
    )
    items = {item["name"]: item for item in dump_schema(view="flat")["items"]}
    assert items["port"]["label"] == "端口号"
    assert items["port"]["key"] == "demo.port"
    assert items["port"]["source"] == "declared+override"
    assert "端口" not in items


def test_manual_group_items_are_cloned_not_aliased(demo):
    """改自动树不能反过来改进程级登记表（否则第二次建树就串味）。"""
    group = setting_group("extra").add(
        setting_item("knob", "extra", "knob", label="旋钮"),
    )
    register_setting_group(group)

    first = build_schema(demo)
    leaf = first.get("extra").get_item("knob")
    assert leaf is not None and leaf.source == "manual"
    leaf.label = "被改过的文案"

    second = build_schema(demo)
    assert second.get("extra").get_item("knob").label == "旋钮"
    # 登记表里的那份也没被污染
    assert _MANUAL_SCHEMA.get("extra").get_item("knob").label == "旋钮"


def test_new_manual_leaf_is_created_when_node_is_absent(demo):
    register_setting_item(
        "demo",
        setting_item("extra_knob", "demo", "extra_knob", label="额外开关"),
    )
    items = {item["key"]: item for item in dump_schema(view="flat")["items"]}
    assert items["demo.extra_knob"]["source"] == "manual"
    assert items["demo.extra_knob"]["label"] == "额外开关"


# ══════════════════════════════════════════════════════════════
# 「先全量校验」的范围（明确写出来，避免消费方误以为跨字段/全有或全无）
# ══════════════════════════════════════════════════════════════

def test_partial_validation_scope_is_documented(demo):
    """只校验**本次给了值**的叶子：没给值的叶子不参与校验。"""
    result = save_values({"demo.port": 2}, config=demo)
    assert [change["key"] for change in result.changes] == ["demo.port"]
    assert result.nodes == (("demo", "port"),)


def test_save_entries_accepts_explicit_entries(demo):
    """``_save_entries`` 的公开契约没变（entries 顺序 = 叶子顺序）。"""
    schema = build_schema(demo)
    entries = [(group, item, group.file) for group, item in schema.iter_items()]
    result = _save_entries(
        entries, {"demo.port": 3}, demo, strict=False, dry_run=False,
    )
    assert result.applied == ("demo.port",)
