#
"""配置结构登记表（软件级）：组合、dump、按归属文件回写。

覆盖：

  1. 多层分组组合 / 父分组挂载 / 重名与未登记父分组的错误
  2. ``SettingItem.from_field`` 从既有 ``ConfigField`` 继承 path / default
  3. ``@setting_group`` 装饰器收集类属性
  4. dump 树结构、当前值、类型、默认值、origin / target 文件
  5. 按归属文件 dump（多 include 文件分别归属）
  6. 格式化文本 dump
  7. 回写：多文件时各节点写回自己的归属文件，不碰主文件
  8. 叶子 / 分组的显式 file 覆盖与继承
  9. 只回写本次标脏的节点
  10. 值转换与控件推导

不依赖 pytest：直接 ``python test/test_setting_schema.py`` 运行；
装了 pytest 也能跑（用例都不带 fixture）。
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue import setup_static_config  # noqa: E402
from albuswall.configue.configue import Configue, StaticConfig  # noqa: E402
from albuswall.configue.resolver import Resolver  # noqa: E402
from albuswall.core import Container  # noqa: E402
from albuswall.configue.setting_schema import (  # noqa: E402
    SettingGroup,
    SettingItem,
    SettingSchemaRegistry,
    dump_setting_schema,
    dump_setting_schema_by_file,
    format_setting_schema_dump,
    save_setting_group,
    setting_group,
    setting_item,
    type_name,
)
from albuswall.ui.builtin.config.static import (  # noqa: E402
    WindowPresenterConfs,
)

# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
_STATE_ATTRS = (
    "resolver", "static", "_default_write_file", "_persistence",
    "_debounce_interval", "_last_flush_error",
)


@contextlib.contextmanager
def _isolated_config():
    """快照 / 还原 Configue 单例的可变状态。"""
    config = Configue()
    snapshot = {name: getattr(config, name) for name in _STATE_ATTRS}
    try:
        yield config
    finally:
        config._cancel_debounce()
        for name, value in snapshot.items():
            setattr(config, name, value)


def _reset_config():
    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit", debounce=0.5)
    return config


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _load(files: dict[str, str], main: str = "main.ini"):
    """写临时 INI 文件并跑 setup_static_config。"""
    tmp = Path(tempfile.mkdtemp(prefix="albuswall-setting-schema-"))
    for name, text in files.items():
        _write(tmp / name, text)
    config = _reset_config()
    setup_static_config(Container(), tmp / main)
    return config, tmp


def _registry_with(*groups: SettingGroup) -> SettingSchemaRegistry:
    registry = SettingSchemaRegistry()
    for group in groups:
        registry.register(group)
    return registry


def _find_item(dump: dict, key: str) -> dict:
    for item in dump["items"]:
        if item["key"] == key:
            return item
    raise AssertionError(
        f"dump 里没有叶子 {key!r}: {[item['key'] for item in dump['items']]}"
    )


# ══════════════════════════════════════════════════════════════
# 1. 组合 / 登记
# ══════════════════════════════════════════════════════════════
def test_nested_groups_compose_and_register() -> None:
    icons = SettingGroup("icons", "图标").add(
        SettingItem("icon_dir", "ui", "icon_dir", label="图标目录"),
    )
    appearance = SettingGroup("appearance", "外观").add(
        SettingItem("theme", "ui", "theme", label="主题"),
        icons,
    )
    general = SettingGroup("general", "常规").add(
        SettingItem("language", "general", "language", label="语言"),
    )

    registry = _registry_with(general, appearance)

    assert registry.group_count == 3
    assert registry.item_count == 3
    assert [root.qualname for root in registry.roots] == [
        "general", "appearance",
    ]
    assert registry.get("appearance.icons") is icons
    assert icons.parent is appearance
    assert icons.qualname == "appearance.icons"
    assert [group.qualname for group in registry.groups()] == [
        "general", "appearance", "appearance.icons",
    ]


def test_register_group_under_existing_parent() -> None:
    registry = SettingSchemaRegistry()
    parent = registry.register(SettingGroup("appearance", "外观"))
    child = registry.register(
        SettingGroup("icons", "图标"),
        parent="appearance",
    )

    assert child.qualname == "appearance.icons"
    assert child.parent is parent
    assert registry.get("appearance.icons") is child


def test_duplicate_group_or_item_is_rejected() -> None:
    registry = SettingSchemaRegistry()
    registry.register(SettingGroup("general", "常规"))

    try:
        registry.register(SettingGroup("general", "重复"))
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复分组应报错")

    group = registry.get("general")
    assert group is not None
    group.add(SettingItem("language", "general", "language"))
    try:
        group.add(SettingItem("language", "general", "language2"))
    except ValueError as exc:
        assert "duplicate item name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("同名叶子应报错")


def test_register_item_under_unregistered_group_raises() -> None:
    registry = SettingSchemaRegistry()
    try:
        registry.register_item(
            SettingItem("language", "general", "language"), "general",
        )
    except KeyError as exc:
        assert "not registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未登记父分组应报错")


def test_replace_registration() -> None:
    registry = SettingSchemaRegistry()
    first = registry.register(SettingGroup("general", "旧"))
    replacement = registry.register(
        SettingGroup("general", "新"), replace=True,
    )

    assert registry.get("general") is replacement
    assert registry.group_count == 1
    assert first.parent is None


def test_configue_package_exports_schema_api() -> None:
    """结构登记是配置层能力，不是 UI 专属：从 albuswall.configue 可取。"""
    import albuswall.configue as configue

    for name in (
        "SETTING_SCHEMA", "SettingGroup", "SettingItem",
        "SettingSchemaRegistry", "setting_group", "setting_item",
        "dump_setting_schema", "dump_setting_schema_by_file",
        "format_setting_schema_dump", "save_setting_group",
        "save_setting_schema",
    ):
        assert getattr(configue, name) is not None


# ══════════════════════════════════════════════════════════════
# 2. 从声明 / 装饰器登记
# ══════════════════════════════════════════════════════════════
def test_item_from_field_inherits_path_and_default() -> None:
    item = SettingItem.from_field(
        "auto_complete", WindowPresenterConfs.auto_complete, label="自动补全",
    )
    assert item.path == ("ui", "auto_complete_theme")
    assert item.default is WindowPresenterConfs.auto_complete.default
    assert item.value_type is bool

    item2 = setting_item(
        "qss", WindowPresenterConfs.qss, label="样式表",
    )
    assert item2.path == ("ui", "style_sheet")
    assert item2.file is None
    # PathField 默认值是字符串，解释器按声明注入后由 resolver 转 Path
    assert item2.default == WindowPresenterConfs.qss.default


def test_setting_group_decorator_collects_items() -> None:
    registry = SettingSchemaRegistry()

    @setting_group(
        "general", title="常规", description="基础设置", registry=registry,
    )
    class GeneralSettings:
        language = setting_item(
            "language", "general", "language", label="语言",
        )
        theme = setting_item("theme", "ui", "theme", label="主题")

    assert isinstance(GeneralSettings.__setting_group__, SettingGroup)
    group = registry.get("general")
    assert group is not None
    assert group.title == "常规"
    assert group.description == "基础设置"
    assert [item.name for item in group.items] == ["language", "theme"]


# ══════════════════════════════════════════════════════════════
# 3. dump
# ══════════════════════════════════════════════════════════════
def test_dump_tree_values_and_origins() -> None:
    config, tmp = _load({
        "appearance.ini": """
            [ui]
            theme = dark
        """,
        "main.ini": """
            [configue]
            include = ./appearance.ini

            [general]
            language = zh_CN
        """,
    })

    icons = SettingGroup("icons", "图标").add(
        SettingItem(
            "icon_dir", "ui", "icon_dir", label="图标目录",
            widget="dir", default="/tmp/icons",
        ),
    )
    appearance = SettingGroup("appearance", "外观").add(
        SettingItem("theme", "ui", "theme", label="主题", order=1),
        icons,
    )
    general = SettingGroup("general", "常规").add(
        SettingItem(
            "language", "general", "language", label="语言",
            widget="choice", choices=("zh_CN", "en_US"),
        ),
    )
    registry = _registry_with(general, appearance)

    dump = dump_setting_schema(config, registry=registry)

    assert dump["summary"] == {
        "group_count": 3, "item_count": 3, "file_count": 2,
    }
    assert [group["key"] for group in dump["groups"]] == [
        "general", "appearance",
    ]
    appearance_node = dump["groups"][1]
    assert appearance_node["item_count"] == 2
    assert [child["key"] for child in appearance_node["groups"]] == [
        "appearance.icons",
    ]

    language = _find_item(dump, "general.language")
    assert language["value"] == "zh_CN"
    assert language["present"] is True
    assert language["widget"] == "choice"
    assert language["choices"] == ["zh_CN", "en_US"]
    assert language["type"] == "str"
    assert language["origin_file"] == str(tmp / "main.ini")
    assert language["target_file"] == str(tmp / "main.ini")

    theme = _find_item(dump, "appearance.theme")
    assert theme["value"] == "dark"
    assert theme["origin_file"] == str(tmp / "appearance.ini")
    assert theme["target_file"] == str(tmp / "appearance.ini")

    icon_dir = _find_item(dump, "appearance.icons.icon_dir")
    # 未在 INI / 声明里出现的节点：用注册时给的默认值，标 missing
    assert icon_dir["value"] == "/tmp/icons"
    assert icon_dir["present"] is False
    assert icon_dir["target_file"] == str(tmp / "main.ini")


def test_dump_item_file_override_and_group_inheritance() -> None:
    config, tmp = _load({
        "main.ini": "[general]\nlanguage = zh_CN\n",
    })
    inherited = tmp / "pref.ini"
    explicit = tmp / "custom.ini"

    group = SettingGroup("general", "常规", file=inherited).add(
        SettingItem("language", "general", "language"),
        SettingItem("theme", "ui", "theme", file=explicit),
    )
    registry = _registry_with(group)
    dump = dump_setting_schema(config, registry=registry)

    language = _find_item(dump, "general.language")
    assert language["target_file"] == str(inherited)
    assert language["explicit_file"] == str(inherited)

    theme = _find_item(dump, "general.theme")
    assert theme["target_file"] == str(explicit)
    assert theme["explicit_file"] == str(explicit)


def test_dump_by_file_groups_items() -> None:
    config, tmp = _load({
        "a.ini": "[general]\nlanguage = zh_CN\n",
        "b.ini": "[appearance]\ntheme = dark\n",
        "main.ini": """
            [configue]
            include = ./a.ini, ./b.ini
        """,
    })

    registry = _registry_with(
        SettingGroup("general", "常规").add(
            SettingItem("language", "general", "language"),
        ),
        SettingGroup("appearance", "外观").add(
            SettingItem("theme", "appearance", "theme"),
        ),
    )
    grouped = dump_setting_schema_by_file(config, registry=registry)

    assert [Path(entry["file"]).name for entry in grouped["files"]] == [
        "a.ini", "b.ini",
    ]
    assert [
        [item["key"] for item in entry["items"]]
        for entry in grouped["files"]
    ] == [["general.language"], ["appearance.theme"]]
    assert grouped["unbound"] == []

    # 把默认落点去掉 → 新节点没有归属文件
    config._default_write_file = None
    registry.register(
        SettingGroup("misc", "其他").add(
            SettingItem("brand_new", "misc", "brand_new"),
        ),
    )
    grouped = dump_setting_schema_by_file(config, registry=registry)
    assert [item["key"] for item in grouped["unbound"]] == [
        "misc.brand_new",
    ]


def test_format_dump_is_readable() -> None:
    config, tmp = _load({
        "main.ini": "[general]\nlanguage = zh_CN\n",
    })
    registry = _registry_with(
        SettingGroup("general", "常规").add(
            SettingItem(
                "language", "general", "language", label="语言",
                choices=("zh_CN", "en_US"),
            ),
        ),
    )

    text = format_setting_schema_dump(config, registry=registry)

    assert "Settings schema: 1 groups, 1 items, 1 files" in text
    assert "常规 [general]" in text
    assert "语言 = 'zh_CN'" in text
    assert "[general]language" in text
    assert "Files:" in text
    assert str(tmp / "main.ini") in text


# ══════════════════════════════════════════════════════════════
# 4. 回写
# ══════════════════════════════════════════════════════════════
def test_save_writes_each_node_to_its_owning_file() -> None:
    config, tmp = _load({
        "general.ini": "[general]\nlanguage = zh_CN\n",
        "appearance.ini": "[ui]\ntheme = dark\n",
        "main.ini": """
            [configue]
            include = ./general.ini, ./appearance.ini
        """,
    })

    registry = _registry_with(
        SettingGroup("general", "常规").add(
            SettingItem("language", "general", "language"),
        ),
        SettingGroup("appearance", "外观").add(
            SettingItem("theme", "ui", "theme"),
        ),
    )

    result = save_setting_group(
        "general",
        {"language": "en_US"},
        config=config,
        registry=registry,
    )
    assert result.written == (tmp / "general.ini",)
    assert result.applied == ("general.language",)
    assert result.flushed == ("general.language",)
    assert "en_US" in (tmp / "general.ini").read_text(encoding="utf-8")

    result = save_setting_group(
        "appearance",
        {"appearance.theme": "light"},
        config=config,
        registry=registry,
    )
    assert result.written == (tmp / "appearance.ini",)
    assert "light" in (tmp / "appearance.ini").read_text(encoding="utf-8")

    main_text = (tmp / "main.ini").read_text(encoding="utf-8")
    assert "language" not in main_text
    assert "theme" not in main_text
    assert config.resolver.dirty_nodes() == frozenset()


def test_save_schema_flushes_all_dirty_nodes_once() -> None:
    config, tmp = _load({
        "general.ini": "[general]\nlanguage = zh_CN\n",
        "appearance.ini": "[ui]\ntheme = dark\n",
        "main.ini": """
            [configue]
            include = ./general.ini, ./appearance.ini
        """,
    })

    registry = _registry_with(
        SettingGroup("general", "常规").add(
            SettingItem("language", "general", "language"),
        ),
        SettingGroup("appearance", "外观").add(
            SettingItem("theme", "ui", "theme"),
        ),
    )

    from albuswall.configue.setting_schema import save_setting_schema

    result = save_setting_schema(
        {
            "general.language": "en_US",
            "appearance.theme": "light",
        },
        config=config,
        registry=registry,
    )

    assert set(result.written) == {tmp / "general.ini", tmp / "appearance.ini"}
    assert set(result.by_file) == {tmp / "general.ini", tmp / "appearance.ini"}
    assert set(result.flushed) == {
        "general.language", "appearance.theme",
    }


def test_save_honors_explicit_file_and_group_file() -> None:
    config, tmp = _load({
        "main.ini": "[general]\nlanguage = zh_CN\n",
    })
    group_file = tmp / "pref.ini"
    item_file = tmp / "custom.ini"

    group = SettingGroup("general", "常规", file=group_file).add(
        SettingItem("language", "general", "language"),
        SettingItem("theme", "ui", "theme", file=item_file),
    )
    registry = _registry_with(group)

    result = save_setting_group(
        group,
        {"language": "en_US", "theme": "dark"},
        config=config,
        registry=registry,
    )

    assert set(result.written) == {group_file, item_file}
    assert set(result.by_file) == {group_file, item_file}
    assert "language = en_US" in group_file.read_text(encoding="utf-8")
    assert "theme = dark" in item_file.read_text(encoding="utf-8")
    main_text = (tmp / "main.ini").read_text(encoding="utf-8")
    assert "language = zh_CN" in main_text


def test_save_without_values_only_flushes_existing_dirty_nodes() -> None:
    config, tmp = _load({
        "main.ini": "[general]\nlanguage = zh_CN\n",
    })
    registry = _registry_with(
        SettingGroup("general", "常规").add(
            SettingItem("language", "general", "language"),
        ),
    )

    result = save_setting_group("general", config=config, registry=registry)
    assert result.written == ()
    assert result.applied == ()

    config.set("general", "language", "en_US")
    result = save_setting_group("general", config=config, registry=registry)
    assert result.written == (tmp / "main.ini",)
    assert "en_US" in (tmp / "main.ini").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 5. 类型 / 控件
# ══════════════════════════════════════════════════════════════
def test_save_validates_before_mutating() -> None:
    config, _ = _load({
        "main.ini": "[misc]\ncount = 1\nother = 2\n",
    })
    registry = _registry_with(
        SettingGroup("misc", "其他").add(
            SettingItem("count", "misc", "count", value_type=int),
            SettingItem("other", "misc", "other", value_type=int),
        ),
    )

    try:
        save_setting_group(
            "misc",
            {"count": "5", "other": "not-an-int"},
            config=config,
            registry=registry,
        )
    except ValueError as exc:
        assert "cannot convert" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法值应整体失败")

    # 第一遍校验失败 → 合法的 count 也不该被写进内存 / 标脏
    assert config.resolver.dirty_nodes() == frozenset()
    assert config.resolver.get_typed("misc", "count") == "1"


def test_coerce_and_widget_inference() -> None:
    number = SettingItem("count", "misc", "count", value_type=int)
    flag = SettingItem("enabled", "misc", "enabled", value_type=bool)
    path = SettingItem("dir", "misc", "dir", value_type=Path)

    assert number.coerce("42") == 42
    assert flag.coerce("true") is True
    assert flag.coerce("false") is False
    assert path.coerce("/tmp/x") == Path("/tmp/x")

    try:
        number.coerce("not-a-number")
    except ValueError as exc:
        assert "cannot convert" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非法 int 应报错")

    try:
        SettingItem("bad", "misc", "bad", widget="selector")
    except ValueError as exc:
        assert "unknown widget" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知控件名应在登记期报错")

    config, _ = _load({"main.ini": "[misc]\ncount = 1\n"})
    registry = _registry_with(
        SettingGroup("misc", "其他").add(number, flag, path),
    )
    dump = dump_setting_schema(config, registry=registry)
    assert _find_item(dump, "misc.count")["widget"] == "int"
    assert _find_item(dump, "misc.enabled")["widget"] == "bool"
    assert _find_item(dump, "misc.dir")["widget"] == "path"


def test_type_name_helpers() -> None:
    from typing import Optional

    assert type_name(bool) == "bool"
    assert type_name(Path) == "Path"
    assert type_name(Optional[str]) == "str | None"
    assert type_name(list[int]) == "list[int]"


# ══════════════════════════════════════════════════════════════
# 6. albusctl 命令层 / CLI
# ══════════════════════════════════════════════════════════════
def test_command_setting_schema_views() -> None:
    _load({"main.ini": "[general]\nlanguage = zh_CN\n"})

    from albuswall.services.command import resolve

    handler = resolve("setting.schema")
    assert handler is not None, "setting.schema 应在命令层注册"

    tree = handler(None)
    assert set(tree) == {"groups", "items", "by_file", "unbound", "summary"}

    by_file = handler(None, view="by_file")
    assert set(by_file) == {"files", "unbound"}

    flat = handler(None, view="flat")
    assert isinstance(flat, list)

    text = handler(None, view="text")
    assert isinstance(text, str)
    assert text.startswith("Settings schema:")

    try:
        handler(None, view="nope")
    except ValueError as exc:
        assert "unknown view" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知 view 应报错")


def test_cli_settings_subcommand_parses() -> None:
    from albuswall.client.cli import _normalize_argv, build_parser

    parser = build_parser()

    args = parser.parse_args(_normalize_argv(["settings"]))
    assert args.command == "settings"
    assert args.view is None
    assert args.json is False

    args = parser.parse_args(_normalize_argv(["--json", "settings"]))
    assert args.command == "settings"
    assert args.json is True

    args = parser.parse_args(
        _normalize_argv(["settings", "--view", "by_file"]),
    )
    assert args.command == "settings"
    assert args.view == "by_file"

    # 普通远端命令名仍会自动补成 call
    args = parser.parse_args(_normalize_argv(["setting.schema", "{}"]))
    assert args.command == "call"
    assert args.method == "setting.schema"


# ══════════════════════════════════════════════════════════════
# 入口（无 pytest 时直接跑）
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
            with _isolated_config():
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
