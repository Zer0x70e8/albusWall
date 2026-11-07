"""设置页样式漂移检查：``#Setting*`` 选择器 ↔ 代码里的 objectName。

两类漂移都在这里变成红灯：

1. **死选择器**：主题里写了 ``#SettingXxx``，但代码里没有任何 ``setObjectName``
   产出它（改名 / 删控件之后留下的僵尸规则）；
2. **裸对象**：代码里给控件起了 ``Setting*`` 的 objectName，主题里却没有对应
   规则（新控件忘了配样式，或者只在内联 QSS 里配过）。

允许清单（``KNOWN_DEAD`` / ``KNOWN_UNSTYLED``）是**显式的债**：新增一条就该
失败，逼你决定是补样式还是补清单；旧的清理掉时也要同步删掉清单项。
"""

from __future__ import annotations

import re
from pathlib import Path

SETTING_PKG = Path("src/albuswall/ui/setting")
THEME_QSS = Path(
    "src/albuswall/resources/themes/default/main_window.qss",
)
SRC = Path("src/albuswall")

#: 主题里存在、但代码里已经不产出该 objectName 的选择器（历史遗留）。
#: 清理主题时同步删掉这里。
KNOWN_DEAD: frozenset[str] = frozenset({
    "SettingAboutKey",
    "SettingAboutValue",
    "SettingCloseButton",
    "SettingDiagnosticsLine",
    "SettingHeader",
    "SettingKeymapAdd",
    "SettingKeymapArgEditor",
    "SettingKeymapArgLabel",
    "SettingKeymapBadge",
    "SettingKeymapEntry",
    "SettingKeymapEntryOrder",
    "SettingKeymapId",
    "SettingKeymapPage",
    "SettingKeymapPicker",
    "SettingKeymapPickerTree",
    "SettingKeymapRow",
    "SettingKeymapSearch",
    "SettingMain",
    "SettingRowActionButton",
    "SettingRowError",
    "SettingRowStatus",
})

#: 代码里有 objectName、但**故意**不配样式（结构容器 / 交给系统默认外观）。
KNOWN_UNSTYLED: frozenset[str] = frozenset({
    "SettingAdvancedToggle",
    "SettingAnchorJumpButton",
    "SettingContent",
    "SettingNav",
    "SettingNavContent",
    "SettingNavScroll",
    "SettingScrollArea",
    "SettingSplitter",
    "SettingsPage",
})


def _object_names(root: Path) -> set[str]:
    """扫描 ``setObjectName("...")``（含动态参数里的字面量）。"""
    names: set[str] = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for call in re.findall(r"setObjectName\((.*?)\)", text, re.S):
            names |= set(re.findall(r'"([^"]+)"', call))
        names |= set(re.findall(r'"object_name":\s*"([^"]+)"', text))
        names |= set(re.findall(r'object_name="([^"]+)"', text))
    return names


def _theme_selectors() -> set[str]:
    return set(re.findall(r"#(Setting\w+)", THEME_QSS.read_text(encoding="utf-8")))


def test_no_new_dead_setting_selectors() -> None:
    dead = _theme_selectors() - _object_names(SRC)
    assert dead <= KNOWN_DEAD, (
        "主题里新增了代码中不存在的 #Setting* 选择器："
        f"{sorted(dead - KNOWN_DEAD)}"
    )


def test_known_dead_selectors_are_still_dead() -> None:
    """清理掉某条死规则后要同步删清单项，否则清单会变成谎话。"""
    dead = _theme_selectors() - _object_names(SRC)
    stale = KNOWN_DEAD - dead
    assert not stale, f"清单里这些选择器已经复活，请从 KNOWN_DEAD 删除：{sorted(stale)}"


def test_every_setting_object_is_styled_or_declared_unstyled() -> None:
    names = {
        name
        for name in _object_names(SETTING_PKG) | _object_names(
            Path("src/albuswall/ui/window"),
        )
        if name.startswith("Setting") or name == "SettingsPage"
    }
    unstyled = names - _theme_selectors()
    assert unstyled <= KNOWN_UNSTYLED, (
        "这些 objectName 没有样式规则，也没登记在 KNOWN_UNSTYLED："
        f"{sorted(unstyled - KNOWN_UNSTYLED)}"
    )
    stale = KNOWN_UNSTYLED & _theme_selectors()
    assert not stale, f"这些对象已经有样式了，请从 KNOWN_UNSTYLED 删除：{sorted(stale)}"
