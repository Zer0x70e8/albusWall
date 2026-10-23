#
"""设置页「布局」——注册与渲染机制，而不是某个控件的摆法。

覆盖：

  1. **外观分组的结构**来自登记表：主题 → 窗口标题 → 主题详情 → 缺失补全，
     每一层都靠 ``order`` 表达（``services/command.py``）；
  2. **混排规则**（``layout.iter_entries``）：叶子 / 自定义行 / 子分组按同一个
     ``order`` 尺度排序，``order`` 相同时保持「叶子 → 自定义行 → 子分组」；
  3. **自定义行注册**（``extras.py``）：重复 id 报错、按分组取、order 排序；
  4. **补丁层把自定义行挂到分组上**（``overrides.apply_patches``），并且
     ``Theme details`` 里有一条 **Edit** 占位按钮；
  5. 渲染层（``groups.py``）只认 ``layout.iter_entries`` 的结果 —— 这里用
     纯数据断言，不创建 ``QApplication``。

不依赖 pytest：直接 ``python test/test_setting_layout.py`` 运行；
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

from albuswall.services.command import (  # noqa: E402
    _setting_schema,
    register_user_settings,
)

# 导入设置页包即登记内置补丁（含 Theme details 的 Edit 行）
from albuswall.ui.builtin.setting import overrides  # noqa: E402
from albuswall.ui.builtin.setting.extras import (  # noqa: E402
    ExtraSpec,
    extras_of,
    iter_extras,
    register_extra,
    unregister_extra,
)
from albuswall.ui.builtin.setting.layout import (  # noqa: E402
    EXTRA,
    GROUP,
    ITEM,
    iter_entries,
)

_APPEARANCE_ITEMS = (
    "appearance.theme",
    "appearance.language",
    "appearance.window_title",
)
_APPEARANCE_GROUPS = ("appearance.theme_details", "appearance.theme_completion")
_THEME_DETAILS_ITEMS = (
    "appearance.theme_details.style_sheet",
    "appearance.theme_details.icon_dir",
    "appearance.theme_details.theme_path",
)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _load_config() -> None:
    """重建全局 Configue 并跑一次静态加载（与真实启动同一条路径）。"""
    from albuswall.configue import setup_static_config
    from albuswall.configue.configue import Configue, StaticConfig
    from albuswall.configue.resolver import Resolver
    from albuswall.core import Container

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit")

    tmp = Path(tempfile.mkdtemp(prefix="albuswall-setting-layout-"))
    main = tmp / "main.ini"
    main.write_text(
        dedent(f"""
            [path]
            theme = {tmp / "theme"}
            cache = {tmp / "cache"}
            temp = {tmp / "temp"}
            data = {tmp / "data"}
        """).lstrip("\n"),
        encoding="utf-8",
    )
    setup_static_config(Container(), main)


def _group(tree: dict, key: str) -> dict:
    """按 qualname 找一层分组 dict。"""
    pending = list(tree.get("groups") or [])
    while pending:
        node = pending.pop()
        if node.get("key") == key:
            return node
        pending.extend(node.get("groups") or [])
    raise AssertionError(f"group {key!r} not found")


def _registry() -> dict:
    """``setting.schema`` 的 tree 视图叠上 UI 补丁（= 设置页看到的结构）。"""
    register_user_settings()
    return overrides.apply_patches(_setting_schema(None, view="tree"))


# ══════════════════════════════════════════════════════════════
# 1. 登记表里的外观分组结构
# ══════════════════════════════════════════════════════════════
def test_appearance_registers_items_then_groups() -> None:
    _load_config()
    tree = _setting_schema(None, view="tree")
    appearance = _group(tree, "appearance")

    assert tuple(item["key"] for item in appearance["items"]) == \
        _APPEARANCE_ITEMS
    assert tuple(child["key"] for child in appearance["groups"]) == \
        _APPEARANCE_GROUPS
    # 顺序是 order 表达的：主题 10 → 语言 15 → 窗口标题 20，子分组 30/40
    assert [item["order"] for item in appearance["items"]] == [10, 15, 20]
    assert [child["order"] for child in appearance["groups"]] == [30, 40]


def test_theme_details_registers_files_and_dir() -> None:
    _load_config()
    tree = _setting_schema(None, view="tree")
    details = _group(tree, "appearance.theme_details")

    assert details["title"] == "Theme details"
    assert tuple(item["key"] for item in details["items"]) == \
        _THEME_DETAILS_ITEMS
    # 样式表 + 图标排在最前，目录跟在后面
    assert [item["order"] for item in details["items"]] == [10, 20, 30]
    # 图标 / 样式表仍然走各自的控件提示
    widgets = {item["name"]: item["widget"] for item in details["items"]}
    assert widgets["style_sheet"] == "file"
    assert widgets["icon_dir"] == "dir"


def test_missing_completion_group_keeps_its_nodes() -> None:
    _load_config()
    tree = _setting_schema(None, view="tree")
    completion = _group(tree, "appearance.theme_completion")

    assert completion["title"] == "Missing completion"
    assert {item["name"] for item in completion["items"]} == {
        "auto_complete", "auto_complete_qss", "auto_complete_icons",
        "auto_complete_only_once",
    }


def test_window_title_is_second_setting() -> None:
    _load_config()
    tree = _setting_schema(None, view="tree")
    items = {item["key"]: item for item in tree["items"]}
    assert items["appearance.window_title"]["widget"] == "text"
    assert items["appearance.window_title"]["label"] == "Window title"
    # 语言在窗口标题**上方**：order 更小
    assert (
        items["appearance.language"]["order"]
        < items["appearance.window_title"]["order"]
    )


def test_moved_items_keep_their_patches() -> None:
    """挪进子分组之后，补丁仍然命中（key 跟着 qualname 走）。"""
    _load_config()
    tree = _registry()
    items = {item["key"]: item for item in tree["items"]}

    stylesheet = items["appearance.theme_details.style_sheet"]
    assert stylesheet["patch"] is not None
    assert stylesheet["hint"] == "Restart required"

    icon_dir = items["appearance.theme_details.icon_dir"]
    assert icon_dir["patch"] is not None
    assert "icon" in icon_dir["description"].lower()

    # 老 key 已经不存在：注册 / 写回都必须用新 key
    assert "appearance.style_sheet" not in items
    assert "appearance.icon_dir" not in items


def test_theme_details_keys_are_writable() -> None:
    """新 key 能直接拿去写回（dry-run 只做解析 / 校验）。"""
    _load_config()
    register_user_settings()
    from albuswall.configue.setting_schema import save_setting_schema

    result = save_setting_schema(
        {"appearance.theme_details.icon_dir": "/tmp/albuswall-icons"},
        dry_run=True,
    )
    keys = {change["key"] for change in result.changes}
    assert "appearance.theme_details.icon_dir" in keys


def test_apply_theme_hook_uses_qualname_keys() -> None:
    """选主题时写回的是「主题详情」子分组下的节点。"""
    _load_config()
    from albuswall.ui.builtin.setting.builtin_patches import apply_theme
    from albuswall.ui.builtin.setting.theme_source import discover_themes

    assert apply_theme("__no_such_theme__", {}) == {
        "appearance.theme": "__no_such_theme__",
    }

    themes = discover_themes()
    if not themes:  # pragma: no cover - 资源目录被裁掉的环境
        return
    info = themes[0]
    mapping = apply_theme(info.name, {})
    assert mapping["appearance.theme"] == info.name
    assert mapping["appearance.theme_details.theme_path"] == str(info.path)
    if info.qss is not None:
        assert mapping["appearance.theme_details.style_sheet"] == \
            str(info.qss)


# ══════════════════════════════════════════════════════════════
# 2. 混排规则（纯数据）
# ══════════════════════════════════════════════════════════════
def test_entries_interleave_items_extras_and_groups() -> None:
    group = {
        "items": [
            {"key": "g.a", "order": 10},
            {"key": "g.b", "order": 50},
        ],
        "extras": [ExtraSpec("g.x", "g", "x", order=30)],
        "groups": [
            {"key": "g.one", "order": 20},
            {"key": "g.two", "order": 40},
        ],
    }
    entries = iter_entries(group)

    # 排序只看 order：10 叶子 → 20 分组 → 30 自定义行 → 40 分组 → 50 叶子
    assert [entry.kind for entry in entries] == [
        ITEM, GROUP, EXTRA, GROUP, ITEM,
    ]
    assert [entry.order for entry in entries] == [10, 20, 30, 40, 50]


def test_entries_keep_legacy_order_without_order_values() -> None:
    """没写 order 时：叶子在前、子分组在后；自定义行默认 order=500 排最后。"""
    group = {
        "items": [{"key": "g.a"}, {"key": "g.b"}],
        "extras": [ExtraSpec("g.x", "g", "x")],
        "groups": [{"key": "g.child"}],
    }
    entries = iter_entries(group)
    assert [entry.kind for entry in entries] == [ITEM, ITEM, GROUP, EXTRA]
    # 显式给自定义行 order 就能插到任意位置
    group["extras"][0] = ExtraSpec("g.x", "g", "x", order=-1)
    assert [entry.kind for entry in iter_entries(group)] == [
        EXTRA, ITEM, ITEM, GROUP,
    ]


def test_entries_sort_is_stable_for_equal_order() -> None:
    group = {
        "items": [{"key": "g.a", "order": 5}, {"key": "g.b", "order": 5}],
        "groups": [],
    }
    assert [entry.payload["key"] for entry in iter_entries(group)] == [
        "g.a", "g.b",
    ]


# ══════════════════════════════════════════════════════════════
# 3. 自定义行注册表
# ══════════════════════════════════════════════════════════════
def test_extra_registration_rejects_duplicates() -> None:
    spec = register_extra(ExtraSpec("layout.dup", "appearance", "Dup"))
    try:
        register_extra(ExtraSpec("layout.dup", "appearance", "Dup again"))
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复登记应抛 ValueError")
    finally:
        unregister_extra("layout.dup")
    assert spec.id == "layout.dup"


def test_extra_registration_validates_fields() -> None:
    try:
        register_extra(ExtraSpec("", "appearance", "x"))
    except ValueError as exc:
        assert "id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 id 应抛 ValueError")

    try:
        register_extra(ExtraSpec("layout.no-group", "", "x"))
    except ValueError as exc:
        assert "group" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 group 应抛 ValueError")


def test_extras_of_filters_and_sorts() -> None:
    register_extra(ExtraSpec("layout.b", "group.test", "B", order=20))
    register_extra(ExtraSpec("layout.a", "group.test", "A", order=10))
    register_extra(ExtraSpec("layout.c", "group.other", "C"))
    try:
        assert [spec.id for spec in extras_of("group.test")] == [
            "layout.a", "layout.b",
        ]
        registered = {
            spec.id for spec in iter_extras()
            if spec.id.startswith("layout.")
        }
        assert registered == {"layout.a", "layout.b", "layout.c"}
        # 全局按 order / id 排序：10、20，其余的默认 500 排后面
        ordered = [
            spec.id for spec in iter_extras()
            if spec.id.startswith("layout.")
        ]
        assert ordered == ["layout.a", "layout.b", "layout.c"]
    finally:
        for extra_id in ("layout.a", "layout.b", "layout.c"):
            unregister_extra(extra_id)


# ══════════════════════════════════════════════════════════════
# 4. 补丁层把自定义行挂到分组上
# ══════════════════════════════════════════════════════════════
def test_theme_details_has_edit_action_row() -> None:
    _load_config()
    tree = _registry()
    details = _group(tree, "appearance.theme_details")

    extras = details.get("extras") or []
    assert [spec.id for spec in extras] == ["theme_details.edit"]
    edit = extras[0]
    assert edit.title == "Edit"
    assert edit.button == "Edit"
    assert edit.order == 40
    # 占位按钮：没有接线
    assert edit.clicked is None


def test_rendered_order_of_theme_details() -> None:
    """渲染层拿到的顺序：样式表 → 图标 → 目录 → Edit 按钮。"""
    _load_config()
    details = _group(_registry(), "appearance.theme_details")
    entries = iter_entries(details)

    assert [entry.kind for entry in entries] == [ITEM, ITEM, ITEM, EXTRA]
    assert [
        entry.payload["key"] if entry.is_item else entry.payload.id
        for entry in entries
    ] == [*_THEME_DETAILS_ITEMS, "theme_details.edit"]


def test_rendered_order_of_appearance() -> None:
    _load_config()
    appearance = _group(_registry(), "appearance")
    entries = iter_entries(appearance)

    assert [entry.kind for entry in entries] == [
        ITEM, ITEM, ITEM, GROUP, GROUP,
    ]
    assert [
        entry.payload["key"] for entry in entries
    ] == [
        "appearance.theme",
        "appearance.language",
        "appearance.window_title",
        "appearance.theme_details",
        "appearance.theme_completion",
    ]


def test_generated_groups_carry_an_extras_field() -> None:
    _load_config()
    tree = _registry()
    for node in _walk_groups(tree["groups"]):
        assert "extras" in node, node["key"]
        assert isinstance(node["extras"], list)


def _walk_groups(groups):
    for node in groups:
        yield node
        yield from _walk_groups(node.get("groups") or [])


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
