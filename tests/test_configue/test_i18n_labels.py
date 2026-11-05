#
"""设置项文案的 i18n（缺口 5）：``label`` / ``description`` 支持 callable。

两半：

* **configue 侧**（无 Qt）：``label`` / ``description`` 可以是
  ``Callable[[], str]``，**在 dump 时求值**；callable 抛异常时不炸 dump；
  普通字符串行为完全不变；
* **UI 侧**（``albuswall.ui.config_i18n``）：``field()`` 把 i18n key 包成
  「每次 dump 都重新翻译」的 callable——切语言后再 dump 就是新文案。
"""

from __future__ import annotations

import os
from contextlib import suppress

import pytest

from albuswall.configue import Configue, configclass, field
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
    ConfigFile,
    reg,
)
from albuswall.configue.schema import (
    SettingGroup,
    SettingItem,
    _resolve_text,
    dump_schema_dict,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .conftest import write_ini  # noqa: E402


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


@pytest.fixture
def config(tmp_path):
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[core]\nlevel = 3\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="manual")
    instance = Configue(base_dir=tmp_path)
    instance.load()
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


def _items(config: Configue) -> dict[str, dict]:
    return {item["name"]: item for item in dump_schema_dict(config)["items"]}


# ==================== _resolve_text ====================

def test_resolve_text_handles_plain_strings() -> None:
    assert _resolve_text("确认") == "确认"
    assert _resolve_text("") == ""
    assert _resolve_text(None) == ""


def test_resolve_text_calls_callables() -> None:
    assert _resolve_text(lambda: "从 callable 来") == "从 callable 来"


def test_resolve_text_swallows_callable_errors() -> None:
    def boom() -> str:
        raise RuntimeError("translation exploded")

    assert _resolve_text(boom) == ""


def test_resolve_text_stringifies_other_values() -> None:
    assert _resolve_text(42) == "42"


# ==================== dump 时求值 ====================

def test_label_callable_is_evaluated_at_dump_time(config: Configue) -> None:
    """同一份声明，两次 dump 得到不同文案（= 没有在装饰期冻结）。"""
    language = {"value": "zh"}

    @configclass("core", "main")
    class Core:
        level: int = field(
            default=3,
            label=lambda: f"等级（{language['value']}）",
            description=lambda: f"调试等级（{language['value']}）",
        )

    try:
        first = _items(config)["level"]
        assert first["label"] == "等级（zh）"
        assert first["description"] == "调试等级（zh）"

        language["value"] = "en"

        second = _items(config)["level"]
        assert second["label"] == "等级（en）"
        assert second["description"] == "调试等级（en）"
    finally:
        _reset_registry()


def test_broken_callable_does_not_break_dump(config: Configue) -> None:
    @configclass("core", "main")
    class Core:
        level: int = field(
            default=3,
            label=lambda: 1 / 0,
            description=lambda: (_ for _ in ()).throw(ValueError("nope")),
        )

    try:
        item = _items(config)["level"]
        # 求值失败 → 文案退化为空，label 再退化为 name
        assert item["label"] == "level"
        assert item["description"] == ""
    finally:
        _reset_registry()


def test_plain_string_labels_are_unchanged(config: Configue) -> None:
    @configclass("core", "main")
    class Core:
        level: int = field(default=3, label="调试等级", description="越大越啰嗦")

    try:
        item = _items(config)["level"]
        assert item["label"] == "调试等级"
        assert item["description"] == "越大越啰嗦"
    finally:
        _reset_registry()


def test_setting_item_display_label_resolves_callable() -> None:
    item = SettingItem(
        name="level", section="core", key="level", label=lambda: "等级",
    )

    assert item.display_label == "等级"


def test_group_description_stays_a_string() -> None:
    """分组文案没有 callable 支持（只有叶子需要 i18n）——不能悄悄坏掉。"""
    group = SettingGroup("core", "核心", description="核心设置")

    assert group.description == "核心设置"


# ==================== UI 侧包装 ====================

def test_ui_field_wraps_the_key_into_a_translator(monkeypatch) -> None:
    from albuswall.ui import config_i18n

    seen: list[str] = []

    def fake_tr(text: str) -> str:
        seen.append(text)
        return f"[{text}]"

    monkeypatch.setattr(config_i18n, "tr", fake_tr)

    spec = config_i18n.field(
        default=1, label="settings.core.label", description="settings.core.desc",
    )

    # 构造期不翻译（还没 dump）
    assert seen == []
    assert callable(spec.label)
    assert spec.label() == "[settings.core.label]"
    assert spec.description() == "[settings.core.desc]"
    assert spec.default == 1


def test_ui_field_leaves_empty_text_alone() -> None:
    from albuswall.ui import config_i18n

    spec = config_i18n.field(default=1)

    assert spec.label == ""
    assert spec.description == ""


def test_ui_field_passes_callables_through() -> None:
    from albuswall.ui import config_i18n

    loader = lambda: "已翻译"  # noqa: E731
    spec = config_i18n.field(label=loader)

    assert spec.label is loader


def test_ui_tr_without_application_returns_source_text() -> None:
    """没有 ``QCoreApplication``（纯逻辑环境）时原样返回源文。"""
    from PySide6.QtCore import QCoreApplication

    from albuswall.ui.config_i18n import tr

    if QCoreApplication.instance() is None:
        assert tr("settings.core.label") == "settings.core.label"
    else:  # 有应用时也必须是字符串（没译文就回退源文）
        assert tr("settings.core.label") == "settings.core.label"
    assert tr("") == ""
