#
"""布局再处理（纯数据）：移动 / 自定义组件 / 序列化。"""

from __future__ import annotations

from albuswall.ui.setting.layout import (
    LayoutClaim,
    SettingLayout,
    apply_layout,
)


def _dump() -> dict:
    return {
        "schema_version": 2,
        "groups": [
            {
                "key": "a",
                "id": "a",
                "title": "A",
                "order": 0,
                "items": [
                    {
                        "key": "a.x",
                        "section": "a",
                        "config_key": "x",
                        "target_file": "/tmp/a.ini",
                        "value": 1,
                    },
                ],
                "groups": [],
                "item_count": 1,
            },
            {
                "key": "b",
                "id": "b",
                "title": "B",
                "order": 1,
                "items": [],
                "groups": [],
                "item_count": 0,
            },
        ],
        "items": [],
        "by_file": {},
        "unbound": [],
        "uncovered": [],
        "summary": {},
    }


def test_move_keeps_leaf_key_and_changes_parent():
    layout = SettingLayout()
    layout.set_move("a.x", "b", order=5)
    result = apply_layout(_dump(), layout)

    groups = {group["key"]: group for group in result["groups"]}
    assert groups["a"]["items"] == []
    assert [item["key"] for item in groups["b"]["items"]] == ["a.x"]
    leaf = groups["b"]["items"][0]
    assert leaf["key"] == "a.x"  # 写回键不变
    assert leaf["order"] == 5

    # 派生字段由 SettingTree 归一化时算一次，layout 不再重复实现
    from albuswall.ui.setting.contract import normalize_dump

    tree = normalize_dump({"schema_version": 2, **result})
    assert tree.by_file == {"/tmp/a.ini": ("a.x",)}
    assert tree.group_of("a.x")["key"] == "b"


def test_custom_group_with_label_component():
    layout = SettingLayout()
    layout.add_component_group(
        "about", title="关于", label="hello", order=999,
    )
    result = apply_layout(_dump(), layout)
    about = next(group for group in result["groups"] if group["key"] == "about")
    assert about["components"][0]["text"] == "hello"

    from albuswall.ui.setting.contract import normalize_dump

    tree = normalize_dump({"schema_version": 2, **result})
    assert tree.summary["group_count"] == 3


def test_layout_round_trip():
    """布局对象只有「纯数据」的序列化形态，不做文件 IO。"""
    layout = SettingLayout()
    layout.set_move("a.x", "b", order=2)
    layout.add_component_group("about", title="关于", label="hi")
    layout.add_claim(LayoutClaim(
        section="ui", key="note", group="ui", name="note", label="备注",
    ))

    loaded = SettingLayout.from_dict(layout.to_dict())
    assert loaded.moves[0].key == "a.x"
    assert loaded.groups[0].components[0].text == "hi"
    assert loaded.claims[0].node == ("ui", "note")


def test_unknown_layout_version_is_ignored():
    assert SettingLayout.from_dict({"version": 99, "moves": []}).moves == []
    assert SettingLayout.from_dict({"version": 99, "moves": []}).groups == []


# ============================================================================
# 内置默认布局（「主题」分组）
# ============================================================================

def _theme_dump() -> dict:
    """够用的最小 dump：``ui.theme`` / ``path.theme`` 各在一个段里。"""
    def group(key: str, items: list[tuple[str, str]], order: int = 0) -> dict:
        return {
            "key": key,
            "id": key,
            "title": key,
            "order": order,
            "items": [
                {
                    "key": name,
                    "section": key,
                    "config_key": name.rsplit(".", 1)[-1],
                    "target_file": "/tmp/config.ini",
                    "type": "str",
                    "value": "default",
                }
                for name, _label in items
            ],
            "groups": [],
        }

    return {
        "schema_version": 2,
        "groups": [
            group("ui", [("ui.theme", "主题")]),
            group("path", [("path.theme", "主题目录")]),
            group("about", [], order=9999),
        ],
    }


def test_builtin_layout_moves_theme_items_into_theme_group():
    from albuswall.ui.setting.contract import normalize_dump
    from albuswall.ui.setting.layout import builtin_layout

    tree = normalize_dump(apply_layout(_theme_dump(), builtin_layout()))
    theme = tree.group("theme")
    assert theme is not None
    assert theme["title"] == "主题"
    assert [leaf.key for leaf in theme.leaves] == ["ui.theme", "path.theme"]
    # 原段还在，只是里面空了
    assert tree.group("ui").leaves == ()
    assert tree.group("path").leaves == ()


def test_user_layout_overrides_builtin_moves():
    from albuswall.ui.setting.contract import normalize_dump
    from albuswall.ui.setting.layout import builtin_layout, merge_layouts

    user = SettingLayout()
    user.set_move("ui.theme", "ui")  # 用户明确要放回 ui 段
    merged = merge_layouts(builtin_layout(), user)
    tree = normalize_dump(apply_layout(_theme_dump(), merged))
    assert tree.group_of("ui.theme")["key"] == "ui"
    assert tree.group_of("path.theme")["key"] == "theme"
    # 同 key 的 move 只有一条（用户覆盖内置，不叠加）
    assert [move.key for move in merged.moves].count("ui.theme") == 1


def test_theme_group_sits_before_about():
    from albuswall.ui.setting.contract import normalize_dump
    from albuswall.ui.setting.layout import builtin_layout

    tree = normalize_dump(apply_layout(_theme_dump(), builtin_layout()))
    keys = [group["key"] for group in tree.groups]
    assert keys.index("theme") < keys.index("about")
