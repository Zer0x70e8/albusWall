#
"""命令层（``services/command.py``）登记的「用户设置树」。

覆盖：

  1. 根分组划分：常规 4 组（外观 / 缩略图 / 导入 / 回收站）+ ``advanced``，
     且 ``advanced`` 排在最后；界面语言是``外观``里的一个叶子
  2. ``advanced`` 子树只装调优 / 路径类，常规分组里不出现
  3. 每个叶子都指向真实存在的配置声明（``ConfigField`` / ``[path]`` 内建）
  4. ``from_field`` 的默认值与声明一致（不重复写默认值）
  5. 保留字段（image_format / quality / specs / xdg_spec_map）不参与编辑
  6. 控件提示：bool / int / float / choice / dir / file 的推导
  7. ``setting.schema`` 的 ``basic`` 视图 = tree 去掉 advanced
  8. 重复登记幂等（``register_user_settings`` 可安全重入）

不依赖 pytest：直接 ``python test/test_user_settings.py`` 运行；
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

# import 即登记设置树（ConfigField 也靠 import 期注册）
from albuswall.configue.declaration import ConfigDeclaration  # noqa: E402
from albuswall.services.command import (  # noqa: E402
    _ADVANCED_GROUP_ID,
    _setting_schema,
    register_user_settings,
)
from albuswall.services.import_ import ImportConfig  # noqa: E402
from albuswall.services.thumbnail.config import ThumbnailConfig  # noqa: E402
from albuswall.ui.bootstrap import UIConfig  # noqa: E402

_BASIC_GROUPS = ("appearance", "thumbnails", "import", "trash")
_ADVANCED_CHILDREN = (
    "paths", "files", "runtime", "logging", "thumbnail_tuning",
    "import_tuning", "trash_tuning", "task", "ipc", "database",
)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-user-settings-"))


def _load_config(extra: str = ""):
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

    tmp = _tmpdir()
    main = tmp / "main.ini"
    main.write_text(
        dedent(f"""
            [path]
            theme = {tmp / "theme"}
            cache = {tmp / "cache"}
            temp = {tmp / "temp"}
            data = {tmp / "data"}
        """ + extra).lstrip("\n"),
        encoding="utf-8",
    )
    setup_static_config(Container(), main)
    return config


def _dump(view: str) -> dict:
    """走真实的 ``setting.schema`` 命令（容器对它无意义）。"""
    return _setting_schema(None, view=view)


# ══════════════════════════════════════════════════════════════
# 1. 分组划分
# ══════════════════════════════════════════════════════════════
def test_root_groups_and_advanced_last() -> None:
    _load_config()
    tree = _dump("tree")

    ids = [group["id"] for group in tree["groups"]]
    assert ids == [*_BASIC_GROUPS, _ADVANCED_GROUP_ID], ids

    # order 保证 advanced 排在最后（前端按 order 渲染）
    orders = [group["order"] for group in tree["groups"]]
    assert orders == sorted(orders), orders


def test_advanced_children() -> None:
    _load_config()
    tree = _dump("tree")
    advanced = next(
        g for g in tree["groups"] if g["id"] == _ADVANCED_GROUP_ID
    )
    assert tuple(g["id"] for g in advanced["groups"]) == _ADVANCED_CHILDREN
    # advanced 有实质内容，不是空壳
    assert advanced["item_count"] > 0


def test_basic_groups_hold_no_tuning_items() -> None:
    """常规分组里只能有「用户日常会改」的字段。"""
    _load_config()
    tree = _dump("tree")
    tuning = {
        "advanced.thumbnail_tuning", "advanced.import_tuning",
        "advanced.task", "advanced.ipc", "advanced.paths",
    }
    for group in tree["groups"]:
        if group["id"] == _ADVANCED_GROUP_ID:
            continue
        for item in group["items"]:
            assert not item["key"].startswith(tuple(tuning)), item["key"]


# ══════════════════════════════════════════════════════════════
# 2. 每个叶子都绑到真实声明
# ══════════════════════════════════════════════════════════════
def test_every_item_points_to_a_declared_node() -> None:
    _load_config()  # 触发 add_builtin（path.* / files）
    # 用 _registry 而不是 metas()：后者按属性名去重，会把两个 batch_size
    # 合成一个（thumbnail / import 各有一个），这里要全部节点的并集。
    declared = {
        (meta["path"][0], meta["path"][1])
        if len(meta["path"]) == 2
        else ("__default__", meta["path"][0])
        for meta in ConfigDeclaration._registry.values()
    }
    tree = _dump("tree")
    missing = [
        f"{item['key']} -> {item['path']}"
        for item in tree["items"]
        if (item["section"], item["config_key"]) not in declared
    ]
    assert not missing, f"这些设置指向了不存在的配置节点: {missing}"


def test_item_defaults_come_from_declarations() -> None:
    _load_config()
    tree = _dump("tree")
    items = {item["key"]: item for item in tree["items"]}

    assert items["thumbnails.batch_size"]["default"] == \
        ThumbnailConfig.batch_size.default
    assert items["import.batch_size"]["default"] == \
        ImportConfig.batch_size.default
    assert items["appearance.theme"]["default"] == UIConfig.theme.default
    # 默认 None 的字段：dump 里也要如实给出 None，而不是漏掉
    assert items["trash.enabled"]["default"] is None
    assert items["advanced.task.max_concurrency"]["default"] is None


def test_values_come_from_the_live_config() -> None:
    _load_config("""
        [thumbnail]
        batch_size = 7
        xdg_enabled = true

        [import]
        batch_size = 700
    """)
    items = {item["key"]: item for item in _dump("tree")["items"]}

    # 同名字段按节点取值，不会串味
    assert items["thumbnails.batch_size"]["value"] == 7
    assert items["import.batch_size"]["value"] == 700
    assert items["thumbnails.xdg_enabled"]["value"] is True
    assert items["thumbnails.xdg_enabled"]["present"] is True


# ══════════════════════════════════════════════════════════════
# 3. 保留字段 / 控件
# ══════════════════════════════════════════════════════════════
def test_reserved_items_are_not_editable() -> None:
    """真正改不动的只有「保留字段」：输出恒为 PNG，改了也没用。"""
    _load_config()
    items = {item["key"]: item for item in _dump("tree")["items"]}
    for key in (
        "advanced.thumbnail_tuning.image_format",
        "advanced.thumbnail_tuning.quality",
    ):
        assert items[key]["editable"] is False, key


def test_dict_items_are_editable_json() -> None:
    """dict 值已支持 JSON 往返，所以可以编辑（用多行文本控件）。"""
    _load_config()
    items = {item["key"]: item for item in _dump("tree")["items"]}
    for key in (
        "advanced.thumbnail_tuning.specs",
        "advanced.thumbnail_tuning.xdg_spec_map",
    ):
        assert items[key]["editable"] is True, key
        assert items[key]["widget"] == "multiline", key


def test_widget_inference() -> None:
    _load_config()
    items = {item["key"]: item for item in _dump("tree")["items"]}

    assert items["thumbnails.xdg_enabled"]["widget"] == "bool"
    assert items["thumbnails.batch_size"]["widget"] == "int"
    assert items["advanced.thumbnail_tuning.write_retry_backoff_sec"][
        "widget"] == "float"
    assert items["advanced.logging.level"]["widget"] == "choice"
    assert items["trash.enabled"]["widget"] == "choice"
    assert items["appearance.theme_details.style_sheet"]["widget"] == \
        "file"
    assert items["thumbnails.thumb_root"]["widget"] == "dir"
    # 约束要能传到前端
    assert items["thumbnails.batch_size"]["constraints"]["minimum"] == 1


def test_locked_dict_items_keep_their_default() -> None:
    _load_config()
    items = {item["key"]: item for item in _dump("tree")["items"]}
    assert items["advanced.thumbnail_tuning.specs"]["default"] == \
        ThumbnailConfig.specs.default
    assert items["advanced.thumbnail_tuning.xdg_spec_map"]["default"] is None


# ══════════════════════════════════════════════════════════════
# 4. 视图
# ══════════════════════════════════════════════════════════════
def test_basic_view_drops_advanced() -> None:
    _load_config()
    tree = _dump("tree")
    basic = _dump("basic")

    assert [g["id"] for g in basic["groups"]] == list(_BASIC_GROUPS)
    assert all(
        not item["group"].startswith(f"{_ADVANCED_GROUP_ID}.")
        for item in basic["items"]
    )
    assert basic["summary"]["item_count"] < tree["summary"]["item_count"]
    assert basic["summary"]["item_count"] == len(basic["items"])


def test_unknown_view_raises() -> None:
    _load_config()
    try:
        _dump("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知 view 应抛 ValueError")


# ══════════════════════════════════════════════════════════════
# 5. 幂等
# ══════════════════════════════════════════════════════════════
def test_registration_is_idempotent() -> None:
    from albuswall.configue.setting_schema import SETTING_SCHEMA

    before = (SETTING_SCHEMA.group_count, SETTING_SCHEMA.item_count)
    assert register_user_settings() is True
    assert register_user_settings() is True
    assert (SETTING_SCHEMA.group_count, SETTING_SCHEMA.item_count) == before


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
