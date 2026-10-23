#
"""设置写回：``setting.set`` / ``setting.save`` + ``dry_run`` + dict 往返。

覆盖：

  1. ``dry_run`` 只预览：不写内存、不写盘，``changes`` 里有 old/new/text/file
  2. 真正 set：值落到声明归属的文件，重新加载后仍在
  3. ``group`` 限定范围时可只用叶子名（如 ``batch_size``）
  4. 未知分组 / 值转不过去 → ``InvalidParams``（不会半截修改）
  5. dict 值（``specs`` / ``xdg_spec_map``）能写回并读回（JSON 表示）
  6. ``setting.save``：dry_run 列出待写节点；实际落盘后 dirty 清空
  7. CLI 的 ``KEY=VALUE`` 解析（数字 / 布尔 / JSON / 字符串）

不依赖 pytest：直接 ``python test/test_setting_write.py`` 运行；
装了 pytest 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.client.cli import _parse_assignments  # noqa: E402
from albuswall.common.ipc import InvalidParams  # noqa: E402
from albuswall.services.command import (  # noqa: E402
    _setting_save,
    _setting_set,
)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _reload(main: Path) -> None:
    """把全局 Configue 重置成干净状态，再从给定文件重新加载。"""
    from albuswall.configue import setup_static_config
    from albuswall.configue.configue import Configue, StaticConfig
    from albuswall.configue.resolver import Resolver
    from albuswall.core import Container

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="manual")
    setup_static_config(Container(), main)


def _load(extra: str = "") -> tuple[Path, Path]:
    """建一个临时主 INI 并跑静态加载，返回 (config_file, tmpdir)。"""
    tmp = Path(tempfile.mkdtemp(prefix="albuswall-setting-write-"))
    main = tmp / "main.ini"
    main.write_text(
        dedent(f"""
            [path]
            theme = {tmp / "theme"}
            cache = {tmp / "cache"}
            temp = {tmp / "temp"}
            data = {tmp / "data"}

            ; 注释必须活下来
            [thumbnail]
            batch_size = 32
        """ + extra).lstrip("\n"),
        encoding="utf-8",
    )
    _reload(main)
    return main, tmp


def _config():
    from albuswall.configue.declaration import get_config

    return get_config()


def _value(section: str, key: str):
    return _config().resolver.get_typed(section, key)


# ══════════════════════════════════════════════════════════════
# 1. dry_run
# ══════════════════════════════════════════════════════════════
def test_dry_run_does_not_touch_memory_or_disk() -> None:
    main, _ = _load()
    before_text = main.read_text(encoding="utf-8")

    result = _setting_set(
        None, values={"thumbnails.batch_size": "64"}, dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["written"] == []
    assert result["applied"] == []
    assert result["ok"] is True

    change = result["changes"][0]
    assert change["key"] == "thumbnails.batch_size"
    assert change["old"] == 32
    assert change["new"] == 64
    assert change["text"] == "64"          # 将写入 INI 的文本
    assert change["file"] == str(main)
    assert change["changed"] is True

    # 内存与磁盘都没动
    assert _value("thumbnail", "batch_size") == 32
    assert main.read_text(encoding="utf-8") == before_text


def test_dry_run_reports_unchanged_values() -> None:
    _load()
    result = _setting_set(
        None, values={"thumbnails.batch_size": "32"}, dry_run=True,
    )
    assert result["changes"][0]["changed"] is False


# ══════════════════════════════════════════════════════════════
# 2. 真正写回
# ══════════════════════════════════════════════════════════════
def test_set_writes_and_survives_reload() -> None:
    main, _ = _load()

    result = _setting_set(None, values={"thumbnails.batch_size": 64})
    assert result["ok"] is True
    assert result["pending"] == []
    assert [Path(p) for p in result["written"]] == [main]
    assert "applied" in result and result["applied"] == [
        "thumbnails.batch_size"
    ]

    text = main.read_text(encoding="utf-8")
    assert "batch_size = 64" in text
    assert "; 注释必须活下来" in text      # 注释保留
    assert _value("thumbnail", "batch_size") == 64

    # 重新加载整份配置：值还在，且能通过类型检查
    _reload(main)
    assert _value("thumbnail", "batch_size") == 64


def test_set_with_group_allows_bare_leaf_name() -> None:
    main, _ = _load()
    result = _setting_set(
        None, group="thumbnails", values={"batch_size": "48"},
    )
    assert result["applied"] == ["thumbnails.batch_size"]
    assert "batch_size = 48" in main.read_text(encoding="utf-8")


def test_set_multiple_groups_at_once() -> None:
    main, _ = _load()
    result = _setting_set(None, values={
        "thumbnails.batch_size": 8,
        "import.batch_size": 900,
        "trash.enabled": "false",
    })
    assert result["ok"] is True
    assert set(result["applied"]) == {
        "thumbnails.batch_size", "import.batch_size", "trash.enabled",
    }
    text = main.read_text(encoding="utf-8")
    assert "batch_size = 8" in text and "batch_size = 900" in text
    assert "xdg_compat = false" in text


# ══════════════════════════════════════════════════════════════
# 3. 错误路径
# ══════════════════════════════════════════════════════════════
def test_unknown_group_is_invalid_params() -> None:
    _load()
    try:
        _setting_set(None, group="nope", values={"batch_size": 1})
    except InvalidParams as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知分组应抛 InvalidParams")


def test_bad_value_is_invalid_params_and_changes_nothing() -> None:
    main, _ = _load()
    before = main.read_text(encoding="utf-8")
    try:
        _setting_set(None, values={"thumbnails.batch_size": "not-a-number"})
    except InvalidParams as exc:
        assert "batch_size" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法值应抛 InvalidParams")
    # 两遍流程保证：校验失败时内存/磁盘都没动
    assert _value("thumbnail", "batch_size") == 32
    assert main.read_text(encoding="utf-8") == before


def test_empty_values_is_invalid_params() -> None:
    _load()
    try:
        _setting_set(None, values={})
    except InvalidParams:
        pass
    else:  # pragma: no cover
        raise AssertionError("空 values 应抛 InvalidParams")


# ══════════════════════════════════════════════════════════════
# 4. dict 值往返
# ══════════════════════════════════════════════════════════════
def test_dict_value_roundtrip() -> None:
    main, _ = _load()
    specs = {"small": 64, "medium": 256, "large": 512}

    result = _setting_set(None, values={"advanced.thumbnail_tuning.specs": specs})
    assert result["ok"] is True, result

    text = main.read_text(encoding="utf-8")
    assert 'specs = {"large": 512, "medium": 256, "small": 64}' in text
    assert _value("thumbnail", "specs") == specs

    # 关键：读回来还能过类型检查（旧实现会写成 Python repr，下次启动炸）
    _reload(main)
    assert _value("thumbnail", "specs") == specs


def test_optional_dict_value_roundtrip() -> None:
    main, _ = _load()
    mapping = {"small": "normal", "large": "xx-large"}
    _setting_set(None, values={"advanced.thumbnail_tuning.xdg_spec_map": mapping})
    assert 'xdg_spec_map = {"large": "xx-large", "small": "normal"}' in \
        main.read_text(encoding="utf-8")
    assert _value("thumbnail", "xdg_spec_map") == mapping


# ══════════════════════════════════════════════════════════════
# 5. setting.save
# ══════════════════════════════════════════════════════════════
def test_save_flushes_dirty_nodes() -> None:
    main, _ = _load()
    config = _config()
    # 模拟「别的代码路径改了值」：直接 set 但 save="defer"
    config.set("thumbnail", "batch_size", 77, save="defer")
    assert config.resolver.dirty_nodes()

    preview = _setting_save(None, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["pending"] == ["[thumbnail]batch_size"]
    assert "batch_size = 77" not in main.read_text(encoding="utf-8")

    result = _setting_save(None)
    assert result["ok"] is True
    assert result["pending"] == []
    assert [Path(p) for p in result["written"]] == [main]
    assert "batch_size = 77" in main.read_text(encoding="utf-8")


def test_save_with_nothing_dirty_is_noop() -> None:
    main, _ = _load()
    before = main.read_text(encoding="utf-8")
    result = _setting_save(None)
    assert result["ok"] is True
    assert result["written"] == []
    assert main.read_text(encoding="utf-8") == before


# ══════════════════════════════════════════════════════════════
# 6. CLI 的 KEY=VALUE 解析
# ══════════════════════════════════════════════════════════════
def test_parse_assignments() -> None:
    parsed = _parse_assignments([
        "batch_size=64",
        "auto_complete=false",
        "theme=dark",
        "thumb_root=/tmp/thumbs",
        'specs={"small": 64}',
    ])
    assert parsed == {
        "batch_size": 64,          # JSON 数字
        "auto_complete": False,    # JSON 布尔
        "theme": "dark",           # 普通字符串
        "thumb_root": "/tmp/thumbs",
        "specs": {"small": 64},    # JSON 对象（dict 值）
    }


def test_parse_assignments_rejects_bad_pairs() -> None:
    for pairs in (["no-equals-sign"], ["=v"], ["a=1", "a=2"]):
        try:
            _parse_assignments(pairs)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"应拒绝 {pairs!r}")


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")

    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
