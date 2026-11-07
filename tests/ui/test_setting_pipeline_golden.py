"""设置页管线 golden：**内置 conf.ini** 走一遍 配置 → present → 树。

这是重构的"地面真值"：不管内部怎么拆，这 64 个键 / 它们的值类别 / 分组
结构都不应该变。任何一个环节（``dump_schema_dict`` / ``apply_present`` /
``SettingTree``）发生漂移，这里会立刻红。

为什么跑**子进程**：``configue`` 的登记表是进程全局的，同一个 pytest 进程里
别的用例（``tests/test_configue``）会往里加声明，dump 形状就跟着变。子进程
= 全新解释器 = 与真实启动一致。

注意：这里**不含** presenter 的再处理（快捷键登记 / about 分组 / 布局），
那些由 ``tests/test_ui_presenter_settings.py`` 覆盖。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

DATA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "albuswall" / "resources" / "config"
)

#: 内置配置走完管线后的**期望形状**（键 → 值类别）。
EXPECTED_WIDGETS: dict[str, str] = {
    "db.must_exist": "bool",
    "files.album_button": "text",
    "files.app_icon": "text",
    "files.db": "text",
    "files.favorite_icon": "text",
    "files.icon_glob": "text",
    "files.log": "text",
    "files.main_window_qss": "text",
    "files.more_button": "text",
    "files.search_button": "text",
    "import.batch_size": "int",
    "import.initial_backoff": "float",
    "import.max_backoff": "float",
    "import.process_timeout": "float",
    "path.cache": "path",
    "path.config": "path",
    "path.data": "path",
    "path.icons": "path",
    "path.temp": "path",
    "path.theme": "path",
    "source_trash.auto_cleanup_on_dirty": "bool",
    "source_trash.debounce": "float",
    "source_trash.orphan_min_age_seconds": "float",
    "source_trash.priority": "int",
    "source_trash.reconciler_name": "text",
    "source_trash.session_marker_path": "text",
    "source_trash.xdg_compat": "bool",
    "task.check_interval": "float",
    "task.cpu_high_threshold": "float",
    "task.cpu_low_threshold": "float",
    "task.initial_max_concurrency": "int",
    "task.low_load_streak": "int",
    "task.max_concurrency": "int",
    "task.max_queue_size": "int",
    "task.mem_high_threshold": "float",
    "task.mem_low_threshold": "float",
    "task.min_concurrency": "int",
    "task.process_workers": "int",
    "task.scheduler_threads": "int",
    "task.submit_timeout": "float",
    "task.wait_warn_threshold": "float",
    "thumbnail.batch_size": "int",
    "thumbnail.failure_cooldown_sec": "float",
    "thumbnail.image_format": "text",
    "thumbnail.quality": "int",
    "thumbnail.specs": "dict",
    "thumbnail.thumb_root": "text",
    "thumbnail.write_max_retries": "int",
    "thumbnail.write_retry_backoff_sec": "float",
    "thumbnail.xdg_enabled": "bool",
    "thumbnail.xdg_spec_map": "dict",
    "ui.album_button": "text",
    "ui.app_icon": "text",
    "ui.favorite_icon": "text",
    "ui.more_button": "text",
    "ui.search_button": "text",
    "ui.style_sheet": "text",
    "ui.theme": "text",
    "window.album_uuid": "text",
    "window.fullscreen": "bool",
    "window.geometry": "text",
    "window.maximized": "bool",
    "window.overlay": "text",
    "window.tile_columns": "int",
}

EXPECTED_GROUPS = (
    "db", "files", "import", "path", "source_trash",
    "task", "thumbnail", "ui", "window",
)

_PIPELINE_SCRIPT = r"""
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from albuswall.configue.bootstrap import setup_config
from albuswall.configue.schema import dump_schema_dict
from albuswall.core import Container
from albuswall.service.command.handlers._present import apply_present
from albuswall.ui.setting.contract import normalize_dump

container = Container()
config = setup_config(
    container, base_dir=Path(sys.argv[1]), portable=True, ignore_missing=True,
)
dump = apply_present(dump_schema_dict(config=config), show_advanced=True)
tree = normalize_dump(dump)
print(json.dumps({
    "schema_version": tree.schema_version,
    "groups": [group["key"] for group in tree.groups],
    "widgets": {leaf.key: leaf["widget"] for leaf in tree.items},
    "summary": dict(tree.summary),
    "files": sorted(tree.by_file),
    "unbound": list(tree.unbound),
}, ensure_ascii=False))
"""


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("golden")
    for name in ("conf.ini", "log_config.ini"):
        source = DATA_DIR / name
        if source.is_file():
            (base / name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8",
            )
    result = subprocess.run(
        [sys.executable, "-c", _PIPELINE_SCRIPT, str(base)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bundled_config_pipeline_shape(pipeline) -> None:
    assert pipeline["schema_version"] == 2
    assert tuple(pipeline["groups"]) == EXPECTED_GROUPS
    assert pipeline["widgets"] == EXPECTED_WIDGETS
    assert pipeline["summary"] == {
        "group_count": len(EXPECTED_GROUPS),
        "item_count": len(EXPECTED_WIDGETS),
        "file_count": len(pipeline["files"]),
        "uncovered_count": pipeline["summary"]["uncovered_count"],
    }
    assert pipeline["unbound"] == []


# ------------------------------------------------------------------ 树自身

def _raw() -> dict:
    return {
        "schema_version": 2,
        "groups": [
            {
                "key": "outer",
                "title": "Outer",
                "order": 0,
                "items": [
                    {
                        "key": "outer.alpha",
                        "section": "outer",
                        "config_key": "alpha",
                        "target_file": "/tmp/a.ini",
                        "type": "int",
                        "value": 1,
                    },
                ],
                "groups": [
                    {
                        "key": "outer.inner",
                        "title": "Inner",
                        "items": [
                            {
                                "key": "outer.inner.beta",
                                "section": "outer",
                                "config_key": "beta",
                                "type": "str",
                                "readonly": True,
                                "value": "x",
                            },
                        ],
                    },
                ],
            },
        ],
        "uncovered": [{"section": "legacy", "key": "unknown"}],
        "source": {"file": "/tmp/a.ini"},
    }


def test_tree_indexes_match_leaves() -> None:
    from albuswall.ui.setting.contract import normalize_dump

    tree = normalize_dump(_raw())
    assert set(tree.leaves) == {"outer.alpha", "outer.inner.beta"}
    assert tree.parent_of("outer.alpha") == "outer"
    assert tree.parent_of("outer.inner.beta") == "outer.inner"
    assert tree.group_of("outer.inner.beta")["key"] == "outer.inner"
    assert tree.group("outer")["item_count"] == 2
    assert dict(tree.by_file) == {"/tmp/a.ini": ("outer.alpha",)}
    assert tree.unbound == ("outer.inner.beta",)
    assert tree.leaf_by_node(("outer", "alpha")) is tree.leaves["outer.alpha"]
    assert [entry["path"] for entry in tree.uncovered] == ["[legacy]unknown"]


def test_tree_is_immutable_and_round_trips() -> None:
    from albuswall.ui.setting.contract import normalize_dump

    tree = normalize_dump(_raw())
    leaf = tree.leaves["outer.alpha"]
    with pytest.raises(TypeError):
        leaf["value"] = 2  # type: ignore[index]
    assert not hasattr(tree.leaves, "clear")  # 只读映射：没有写接口

    again = normalize_dump(tree.to_dump())
    assert {k: dict(v) for k, v in again.leaves.items()} == {
        k: dict(v) for k, v in tree.leaves.items()
    }
