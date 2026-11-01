#
"""``configue.backends.json``：read / write / resolve / serialize。

JSON 后端的三个坑（都有用例）：

1. 值是 JSON 原生类型，不是字符串（``true`` ≠ ``"true"``）；
2. 写回是「读全量 → 改 → 整体写回」，必须保序（``sort_keys``）且不丢别人的 section；
3. 结构不对（根 / section 不是对象、JSON 坏了）要报 ``ConfigError`` 而不是崩。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import pytest

from albuswall.configue import ConfigError, ConfigFile, get_backend
from albuswall.configue.backends.ini import DELETE
from albuswall.configue.backends.json import JSONBackend
from albuswall.configue.errors import AmbiguousConfigFile, ConfigFileNotFound

from .conftest import read_json, write_json


@pytest.fixture
def backend() -> JSONBackend:
    return JSONBackend()


# ==================== 注册表 ====================

def test_json_backend_is_registered():
    assert isinstance(get_backend(ConfigFile.JSON), JSONBackend)


# ==================== read ====================

def test_read_returns_typed_values(tmp_path, backend):
    write_json(
        tmp_path / "a.json",
        {"core": {"debug": True, "port": 80, "paths": ["/a"], "meta": {"x": 1}}},
    )

    raw = backend.read(tmp_path / "a.json")

    assert raw == {
        ("core", "debug"): True,
        ("core", "port"): 80,
        ("core", "paths"): ["/a"],
        ("core", "meta"): {"x": 1},
    }


def test_read_missing_file_returns_empty(tmp_path, backend):
    assert backend.read(tmp_path / "nope.json") == {}


def test_read_empty_file_returns_empty(tmp_path, backend):
    (tmp_path / "empty.json").write_text("{}", encoding="utf-8")
    assert backend.read(tmp_path / "empty.json") == {}


def test_read_invalid_json_raises(tmp_path, backend):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid JSON"):
        backend.read(tmp_path / "bad.json")


def test_read_root_not_object_raises(tmp_path, backend):
    write_json(tmp_path / "a.json", [1, 2, 3])

    with pytest.raises(ConfigError, match="must be an object"):
        backend.read(tmp_path / "a.json")


def test_read_section_not_object_raises(tmp_path, backend):
    write_json(tmp_path / "a.json", {"core": 1})

    with pytest.raises(ConfigError, match="must be an object"):
        backend.read(tmp_path / "a.json")


# ==================== write ====================

def test_write_replaces_and_keeps_other_sections(tmp_path, backend):
    target = write_json(
        tmp_path / "a.json",
        {"core": {"port": 80}, "other": {"keep": True}},
    )

    backend.write(target, {"core": {"port": 8080}})

    assert read_json(target) == {"core": {"port": 8080}, "other": {"keep": True}}


def test_write_adds_new_section_and_key(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {"core": {"a": 1}})

    backend.write(target, {"core": {"b": 2}, "fresh": {"c": 3}})

    assert read_json(target) == {"core": {"a": 1, "b": 2}, "fresh": {"c": 3}}


def test_write_delete_removes_key_and_empty_section(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {"core": {"port": 80}, "keep": {"x": 1}})

    backend.write(target, {"core": {"port": DELETE}})

    assert read_json(target) == {"keep": {"x": 1}}


def test_write_keeps_empty_section_that_was_not_edited(tmp_path, backend):
    """用户手写的空 section 不属于本次 edits → 不被顺手清掉。"""
    target = write_json(
        tmp_path / "a.json", {"core": {"a": 1}, "empty": {}},
    )

    backend.write(target, {"core": {"a": 2}})

    assert read_json(target) == {"core": {"a": 2}, "empty": {}}


def test_write_is_stable_and_pretty(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {"b": {"z": 1, "a": 2}})

    backend.write(target, {"b": {"c": 3}})

    text = target.read_text(encoding="utf-8")
    assert "  " in text                      # indent=2
    assert text.endswith("\n")               # 结尾换行
    assert text.index('"a"') < text.index('"c"') < text.index('"z"')  # sort_keys


def test_write_keeps_unicode_readable(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {})

    backend.write(target, {"core": {"name": "壁纸"}})

    text = target.read_text(encoding="utf-8")
    assert "壁纸" in text                    # ensure_ascii=False
    assert read_json(target) == {"core": {"name": "壁纸"}}


def test_write_creates_parent_dirs(tmp_path, backend):
    target = tmp_path / "deep" / "dir" / "a.json"
    backend.write(target, {"core": {"a": 1}})

    assert read_json(target) == {"core": {"a": 1}}


def test_write_invalid_existing_file_raises_without_destroying_it(tmp_path, backend):
    target = tmp_path / "a.json"
    target.write_text("{broken", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid JSON"):
        backend.write(target, {"core": {"a": 1}})

    assert target.read_text(encoding="utf-8") == "{broken"


def test_write_section_type_conflict_raises(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {"core": 1})

    with pytest.raises(ConfigError, match="not an object"):
        backend.write(target, {"core": {"a": 1}})


# ==================== resolve ====================

def test_resolve_exact_and_suffix(tmp_path, backend):
    target = write_json(tmp_path / "a.json", {})

    assert backend.resolve(target) == target
    assert backend.resolve(tmp_path / "a") == target
    assert backend.resolve(tmp_path / "a.conf") == target


def test_resolve_missing_raises(tmp_path, backend):
    with pytest.raises(ConfigFileNotFound):
        backend.resolve(tmp_path / "nope")


def test_resolve_ambiguous_raises(tmp_path, backend):
    write_json(tmp_path / "a.json", {})
    write_json(tmp_path / "a.json.json", {})

    with pytest.raises(AmbiguousConfigFile):
        backend.resolve(tmp_path / "a.json")


# ==================== serialize ====================

class Colour(Enum):
    RED = "red"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (80, 80),
        (1.5, 1.5),
        ("x", "x"),
        (Path("/tmp/x"), "/tmp/x"),
        (Colour.RED, "red"),
        ([1, 2], [1, 2]),
        ((1, 2), [1, 2]),
        ({"a": 1}, {"a": 1}),
        ({3, 1, 2}, [1, 2, 3]),
    ],
)
def test_serialize_to_json_native(backend, value, expected):
    assert backend.serialize(value, None) == expected


def test_serialize_nested_containers(backend):
    value = {"paths": [Path("/a"), Path("/b")], "flags": [True, None]}

    assert backend.serialize(value, dict) == {
        "paths": ["/a", "/b"],
        "flags": [True, None],
    }


def test_serialize_enum_value_not_name(backend):
    assert backend.serialize(Colour.RED, Colour) == "red"


def test_serialize_uses_custom_serializer(backend):
    class Colour2:
        def __init__(self, name: str) -> None:
            self.name = name

    assert backend.serialize(
        Colour2("red"), Colour2, custom_serializers={Colour2: lambda c: c.name},
    ) == "red"


def test_serialize_uses_to_ini(backend):
    class WithToIni:
        def to_ini(self) -> str:
            return "custom"

    assert backend.serialize(WithToIni(), WithToIni) == "custom"


def test_serialize_unserializable_raises(backend):
    class Opaque:
        pass

    with pytest.raises(ConfigError, match="cannot serialize"):
        backend.serialize(Opaque(), Opaque)


def test_serialize_bad_to_ini_return_raises(backend):
    class BadToIni:
        def to_ini(self) -> int:  # type: ignore[override]
            return 1

    with pytest.raises(ConfigError, match="must return str"):
        backend.serialize(BadToIni(), BadToIni)


def test_serialize_set_order_is_stable(backend):
    value = {"b", "a", "c"}
    assert backend.serialize(value, set) == backend.serialize(value, set)


def test_serialize_json_dump_roundtrip(tmp_path, backend):
    """serialize 的结果必须能直接 json.dump（否则 write 会在运行期炸）。"""
    value = {"a": [Path("/x"), Colour.RED], "b": None}
    native = backend.serialize(value, dict)

    text = json.dumps(native)
    assert json.loads(text) == {"a": ["/x", "red"], "b": None}
