#
"""``configue1.backends.ini``：read / write / resolve / serialize 单个后端。"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue1 import ConfigError, ConfigFile, get_backend
from albuswall.configue1.backends import resolve_candidates
from albuswall.configue1.backends.ini import DELETE, INIBackend
from albuswall.configue1.registry import ConfigFileNotFound

from .conftest import write_ini


@pytest.fixture
def backend() -> INIBackend:
    return INIBackend()


# ==================== 注册表 ====================

def test_ini_backend_is_registered():
    assert isinstance(get_backend(ConfigFile.INI), INIBackend)


# ==================== read ====================

def test_read_returns_section_key_pairs(tmp_path, backend):
    write_ini(tmp_path / "a.ini", "[core]\ndebug = true\nport = 80\n")

    raw = backend.read(tmp_path / "a.ini")

    assert raw == {("core", "debug"): "true", ("core", "port"): "80"}


def test_read_missing_file_returns_empty(tmp_path, backend):
    assert backend.read(tmp_path / "nope.ini") == {}


def test_read_file_without_section_header_uses_default_section(tmp_path, backend):
    write_ini(tmp_path / "a.ini", "key = value\n")

    assert backend.read(tmp_path / "a.ini") == {("__default__", "key"): "value"}


# ==================== write ====================

def test_write_replaces_and_appends(tmp_path, backend):
    target = write_ini(tmp_path / "a.ini", "[core]\nport = 80\n")
    backend.write(target, {"core": {"port": "8080", "host": "localhost"}})

    text = target.read_text(encoding="utf-8")
    assert "port = 8080" in text
    assert "host = localhost" in text


def test_write_delete_removes_key(tmp_path, backend):
    target = write_ini(tmp_path / "a.ini", "[core]\nport = 80\nhost = x\n")
    backend.write(target, {"core": {"port": DELETE}})

    text = target.read_text(encoding="utf-8")
    assert "port" not in text
    assert "host = x" in text


def test_write_preserves_comments_and_crlf(tmp_path, backend):
    target = tmp_path / "a.ini"
    target.write_bytes(b"# head\r\n[core]\r\nport = 80\r\n")

    backend.write(target, {"core": {"port": "81"}})

    data = target.read_bytes()
    assert b"# head" in data
    assert b"port = 81" in data
    assert b"\r\n" in data


def test_write_creates_parent_dirs(tmp_path, backend):
    target = tmp_path / "deep" / "dir" / "a.ini"
    backend.write(target, {"core": {"a": "1"}})

    assert target.exists()
    assert backend.read(target) == {("core", "a"): "1"}


def test_write_rejects_non_str_values(tmp_path, backend):
    target = write_ini(tmp_path / "a.ini", "[core]\n")
    with pytest.raises(TypeError, match="INI backend expects str"):
        backend.write(target, {"core": {"port": 80}})


# ==================== resolve ====================

def test_resolve_exact_file(tmp_path, backend):
    target = write_ini(tmp_path / "a.ini", "[core]\n")
    assert backend.resolve(target) == target


def test_resolve_adds_ini_suffix(tmp_path, backend):
    target = write_ini(tmp_path / "a.ini", "[core]\n")

    assert backend.resolve(tmp_path / "a") == target
    assert backend.resolve(tmp_path / "a.conf") == target


def test_resolve_exact_plus_stem_candidate_is_ambiguous(tmp_path, backend):
    """``a.conf`` 与 ``a.ini`` 同时存在 → 两个候选都命中，报歧义而不是猜。"""
    write_ini(tmp_path / "a.conf", "[core]\nx = 1\n")
    write_ini(tmp_path / "a.ini", "[core]\nx = 2\n")

    with pytest.raises(Exception) as ei:
        backend.resolve(tmp_path / "a.conf")
    assert "ambiguous" in str(ei.value).lower()


def test_resolve_missing_raises(tmp_path, backend):
    with pytest.raises(ConfigFileNotFound):
        backend.resolve(tmp_path / "nope")


def test_resolve_ambiguous_raises(tmp_path, backend):
    write_ini(tmp_path / "a.conf", "[core]\n")
    write_ini(tmp_path / "a.conf.ini", "[core]\n")

    with pytest.raises(Exception) as ei:
        backend.resolve(tmp_path / "a.conf")
    assert "ambiguous" in str(ei.value).lower()


def test_shared_resolve_candidates_dedupes(tmp_path):
    """``a`` 的三个候选里 ``a.ini`` 出现两次，去重后只算一个。"""
    write_ini(tmp_path / "a.ini", "[core]\n")

    assert resolve_candidates(tmp_path / "a", ".ini") == tmp_path / "a.ini"


# ==================== serialize ====================

@pytest.mark.parametrize(
    ("value", "converter", "expected"),
    [
        (True, bool, "true"),
        (False, bool, "false"),
        (80, int, "80"),
        (1.5, float, "1.5"),
        ("x", str, "x"),
        (None, None, ""),
        (Path("/tmp/x"), Path, "/tmp/x"),
        (["a", "b"], list[str], "a, b"),
    ],
)
def test_serialize_to_ini_text(backend, value, converter, expected):
    assert backend.serialize(value, converter) == expected


def test_serialize_uses_custom_serializer(backend):
    class Color:
        def __init__(self, name: str) -> None:
            self.name = name

    text = backend.serialize(
        Color("red"),
        Color,
        custom_serializers={Color: lambda c: c.name},
    )
    assert text == "red"


def test_serialize_unserializable_raises(backend):
    class Opaque:
        pass

    with pytest.raises((ConfigError, ValueError)):
        backend.serialize(Opaque(), Opaque)
