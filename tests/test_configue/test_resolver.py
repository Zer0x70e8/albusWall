#
"""``configue.resolver``：origin / definitions / dirty / forget_file。"""

from __future__ import annotations

from pathlib import Path

from albuswall.configue.resolver import Resolver

LOW = Path("/tmp/low.ini")
HIGH = Path("/tmp/high.ini")


def test_inject_raw_tracks_origin_and_definitions_in_order():
    r = Resolver()
    r.inject_raw("core", "name", "low", origin=LOW, overwrite=True)
    r.inject_raw("core", "name", "high", origin=HIGH, overwrite=True)

    assert r.get_str("core", "name") == "high"          # 后注入的赢
    assert r.origin_of("core", "name") == HIGH
    assert r.definitions_of("core", "name") == (LOW, HIGH)  # 低 → 高


def test_inject_raw_without_overwrite_keeps_first_value_but_records_definition():
    r = Resolver()
    r.inject_raw("core", "name", "low", origin=LOW)
    r.inject_raw("core", "name", "high", origin=HIGH, overwrite=False)

    assert r.get_str("core", "name") == "low"
    assert r.origin_of("core", "name") == LOW
    assert r.definitions_of("core", "name") == (LOW, HIGH)


def test_set_marks_dirty_and_mark_clean_clears():
    r = Resolver()
    r.set("core", "port", 1)
    assert r.dirty_nodes() == frozenset({("core", "port")})

    r.mark_clean([("core", "port")])
    assert r.dirty_nodes() == frozenset()


def test_set_persist_false_does_not_mark_dirty():
    r = Resolver()
    r.set("core", "port", 1, persist=False)
    assert r.dirty_nodes() == frozenset()
    assert r.get_typed("core", "port") == 1


def test_forget_file_falls_back_to_remaining_definition():
    r = Resolver()
    r.inject_raw("core", "name", "low", origin=LOW, overwrite=True)
    r.inject_raw("core", "name", "high", origin=HIGH, overwrite=True)

    removed = r.forget_file(HIGH)

    assert removed == frozenset({("core", "name")})
    # 值回退到 LOW 自己写的那个值，而不是直接消失
    assert r.get_str("core", "name") == "low"
    assert r.origin_of("core", "name") == LOW
    # 定义记录里只剩 LOW
    assert r.definitions_of("core", "name") == (LOW,)


def test_forget_file_drops_definition_entry_when_last_contributor():
    r = Resolver()
    r.inject_raw("core", "port", "1", origin=LOW, overwrite=True)

    r.forget_file(LOW)

    assert r.definitions_of("core", "port") == ()
    assert ("core", "port") not in r


def test_forget_file_keeps_nodes_from_other_files():
    r = Resolver()
    r.inject_raw("core", "a", "1", origin=LOW, overwrite=True)
    r.inject_raw("core", "b", "2", origin=HIGH, overwrite=True)

    r.forget_file(HIGH)

    assert r.get_str("core", "a") == "1"
    assert ("core", "b") not in r


def test_forget_file_unknown_path_is_noop():
    r = Resolver()
    r.inject_raw("core", "a", "1", origin=LOW, overwrite=True)

    assert r.forget_file(Path("/tmp/never.ini")) == frozenset()
    assert r.get_str("core", "a") == "1"


def test_forget_file_keeps_originless_defaults():
    r = Resolver()
    r.inject_raw("core", "a", "default")  # 默认值没有 origin
    r.inject_raw("core", "a", "file", origin=LOW, overwrite=True)

    r.forget_file(LOW)

    # 默认值曾经被文件值覆盖，撤掉文件值后只剩「什么都不剩」
    assert ("core", "a") not in r
