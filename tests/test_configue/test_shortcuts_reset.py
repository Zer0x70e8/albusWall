#
"""「恢复默认」（缺口 3）：``Configue.reset`` + ``Resolver._reset_pending``。

三种场景：

* **双文件**（随包默认 + 用户覆盖）：``reset`` 让内存回退默认值，``flush``
  把用户文件里那一行删掉（``DELETE``），默认文件不动；
* **只有用户文件**：``reset`` 让节点从 Resolver 消失，``flush`` 删行；
* **只有只读定义**：本来就是默认值 → ``reset`` 什么也不做（不会把值抹掉）。

再加上「reset 之后又 set」与「reset 广播 → 快捷键回到默认」两条链路。
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.common.keymap import SECTION, ShortcutRecord
from albuswall.configue import Configue
from albuswall.configue.bootstrap import (
    SHORTCUT_DEFAULTS_FILE,
    SHORTCUT_USER_FILE,
    setup_config,
)
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
    ConfigFile,
    reg,
)
from albuswall.core import Container

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .conftest import write_ini  # noqa: E402


def _reset_registry(*, redeclare: bool = True) -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()
    if not redeclare:
        return
    # 清表会把 import 期完成的 ``@configclass`` 一起清掉（声明是导入副作用，
    # 不会重跑），而 ``[ui] theme`` 的**声明默认值**是 reset 的兜底依据，
    # 所以这里重放一次（与真实启动顺序：先 import 声明、再 setup_config 等价）。
    from albuswall.configue import configclass
    from albuswall.ui.presenter.theme import ThemeConfig

    configclass("ui", "main")(ThemeConfig)


@pytest.fixture
def config(tmp_path: Path):
    """portable 配置：随包默认表 + 用户表（``app.quit`` 被覆盖）。"""
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(
        tmp_path / "shortcuts.ini",
        f"[{SECTION}]\napp.quit = [\"Ctrl+Alt+Q\"]\n",
    )
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


def _user_text(tmp_path: Path) -> str:
    return (tmp_path / "shortcuts.ini").read_text(encoding="utf-8")


# ==================== 双文件场景 ====================

def test_reset_falls_back_to_the_bundled_default(
        config: Configue, tmp_path: Path,
) -> None:
    assert config.resolver.get_typed(SECTION, "app.quit").keys == ("Ctrl+Alt+Q",)

    changed = config.reset(SECTION, "app.quit")

    assert changed is True
    # 内存：回退到随包默认值
    assert config.resolver.get_typed(SECTION, "app.quit").keys == ("Ctrl+Q",)
    # 磁盘：用户文件里那一行被删掉，默认文件还在
    config.flush(file=SHORTCUT_USER_FILE)
    assert "app.quit" not in _user_text(tmp_path)


def test_reset_does_not_touch_the_bundled_table(
        config: Configue, tmp_path: Path,
) -> None:
    bundled = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path
    assert bundled is not None
    before = bundled.read_bytes()

    config.reset(SECTION, "app.quit")
    config.flush()

    assert bundled.read_bytes() == before


def test_reset_marks_and_then_clears_the_pending_flag(config: Configue) -> None:
    config.reset(SECTION, "app.quit")
    assert config.resolver.is_reset_pending((SECTION, "app.quit")) is True

    config.flush(file=SHORTCUT_USER_FILE)

    assert config.resolver.is_reset_pending((SECTION, "app.quit")) is False
    assert config.dirty_nodes() == frozenset()


def test_reset_emits_a_reset_event(config: Configue) -> None:
    events: list[tuple[tuple[str, str], object, str]] = []
    config.subscribe(lambda node, value, source: events.append(
        (node, value, source)))

    config.reset(SECTION, "app.quit")

    assert events
    node, value, source = events[0]
    assert node == (SECTION, "app.quit")
    assert source == "reset"
    assert value.keys == ("Ctrl+Q",)


def test_reset_is_a_noop_for_a_readonly_only_node(
        config: Configue,
) -> None:
    """只被随包默认表定义过的 key：本来就是默认值，reset 不能把它抹掉。"""
    before = config.resolver.get_typed(SECTION, "view.reset")

    changed = config.reset(SECTION, "view.reset")

    assert changed is False
    assert config.resolver.get_typed(SECTION, "view.reset") == before
    assert config.resolver.is_reset_pending((SECTION, "view.reset")) is False


def test_reset_returns_false_without_origin(config: Configue) -> None:
    """没有 origin 的节点：没有「用户那一行」可删，reset 什么也不做。"""
    assert config.reset("nope", "nothing") is False


def test_reset_of_a_declared_field_returns_the_declaration_default(
        config: Configue, tmp_path: Path,
) -> None:
    """声明字段（``@configclass`` 带默认值）：回到声明默认值并摘掉 origin。

    ``[ui] theme`` 的声明默认值是 ``default``；用户在 config.ini 里写过一行
    时 reset 应当把内存值退回声明默认值、``flush`` 删掉文件里那一行 ——
    而不是让节点消失（消失会让 ``ThemeConfig().theme`` 抛
    ``ConfigMissingError``）。
    """
    from albuswall.ui.presenter.theme import ThemeConfig

    assert config.resolver.get_typed("ui", "theme") == "default"
    assert config.resolver.origin_of("ui", "theme") is not None

    assert config.reset("ui", "theme") is True

    assert ThemeConfig().theme == "default"        # 读得到，且是声明默认值
    assert config.resolver.origin_of("ui", "theme") is None

    config.flush(file="main")
    assert "theme" not in (tmp_path / "config.ini").read_text(encoding="utf-8")


def test_set_after_reset_writes_the_new_value(
        config: Configue, tmp_path: Path,
) -> None:
    """reset 之后又显式设值：``_reset_pending`` 清空，flush 写新值而不是删行。"""
    config.reset(SECTION, "app.quit")
    assert config.resolver.is_reset_pending((SECTION, "app.quit")) is True

    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+R",)))

    assert config.resolver.is_reset_pending((SECTION, "app.quit")) is False
    config.flush(file=SHORTCUT_USER_FILE)
    text = _user_text(tmp_path)
    assert "app.quit" in text
    assert "Ctrl+Alt+R" in text


def test_reset_persist_false_only_touches_memory(config: Configue) -> None:
    changed = config.reset(SECTION, "app.quit", persist=False)

    assert changed is True
    assert config.dirty_nodes() == frozenset()
    assert config.resolver.get_typed(SECTION, "app.quit").keys == ("Ctrl+Q",)


# ==================== reset → set → flush（安全链路） ====================

def test_reset_then_set_then_flush_writes_user_file(
        config: Configue, tmp_path: Path,
) -> None:
    """分支 2 之后 ```set``` 不改 origin：flush 靠段级兜底落用户表，不抛错。

    reset 把 ``app.quit`` 的 origin 退到**只读**的随包默认表；``set`` 只改值
    不改 origin，于是 ``target_file_of`` 必须能从段级映射推出可写文件。
    """
    assert config.reset(SECTION, "app.quit") is True
    assert config.resolver.origin_of(SECTION, "app.quit") == _FILES[
        SHORTCUT_DEFAULTS_FILE
    ].resolved_path
    assert config.is_readonly_path(
        config.resolver.origin_of(SECTION, "app.quit")) is True

    config.resolver.set(
        SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+R",)))

    written = config.flush(file=SHORTCUT_USER_FILE)

    assert written == [config.base_dir / "shortcuts.ini"]
    assert config.last_flush_error is None
    text = _user_text(tmp_path)
    assert "app.quit" in text and "Ctrl+Alt+R" in text
    # 随包默认表只读：一个字节都不许变
    bundled = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path
    assert bundled is not None
    assert "Ctrl+Alt+R" not in bundled.read_text(encoding="utf-8")


def test_reset_then_set_then_flush_for_a_declared_field(
        config: Configue, tmp_path: Path,
) -> None:
    """分支 3 之后 ```set```：origin 仍是 None，flush 靠 ``_FIELD_OWNER`` 落主文件。

    ``[ui] theme`` 是 ``@configclass`` 声明的字段：reset 回到声明默认值并
    清空 origin，``set`` 之后 ``target_file_of`` 走 ``_FIELD_OWNER``（= main），
    写的是**新值**而不是 DELETE（``set`` 已经把 ``_reset_pending`` 清掉）。
    """
    from albuswall.ui.presenter.theme import ThemeConfig

    assert config.reset("ui", "theme") is True
    assert config.resolver.origin_of("ui", "theme") is None
    assert ThemeConfig().theme == "default"        # 声明默认值

    config.resolver.set("ui", "theme", "dusk")
    assert config.resolver.is_reset_pending(("ui", "theme")) is False

    written = config.flush(file="main")

    assert written == [config.base_dir / "config.ini"]
    assert config.last_flush_error is None
    assert "dusk" in (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert ThemeConfig().theme == "dusk"


# ==================== 只有用户文件 ====================

@pytest.fixture
def user_only_config(tmp_path: Path):
    """一份只有用户文件（没有随包默认表）的配置。

    这里**不重放** ``[ui]`` 的声明：本用例根本不登记 ``main`` 文件，带着
    「声明了 main 却没 reg」会让 ``load()`` 在 Step 1 直接报 unregistered_file。
    """
    previous = Configue._instance
    _reset_registry(redeclare=False)
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(
        tmp_path / "shortcuts.ini",
        '[shortcuts]\nonly.user = ["Ctrl+Alt+U"]\n',
    )
    reg(ConfigFile.INI, "shortcuts.ini", name="shortcuts", autosave="manual")
    instance = Configue(base_dir=tmp_path)
    instance.load()
    instance.register_section_file("shortcuts", "shortcuts")
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry(redeclare=False)


def test_reset_drops_the_node_when_nothing_is_left(
        user_only_config: Configue, tmp_path: Path,
) -> None:
    config = user_only_config
    assert (SECTION, "only.user") in config.resolver

    assert config.reset(SECTION, "only.user") is True

    assert (SECTION, "only.user") not in config.resolver

    config.flush(file="shortcuts")
    assert "only.user" not in (tmp_path / "shortcuts.ini").read_text(
        encoding="utf-8")


# ==================== 与快捷键注册表的联动 ====================

def test_reset_restores_the_default_keys(config: Configue) -> None:
    """reset 广播 → 注册表重建 → 快捷键回到默认。"""
    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    calls: list[str] = []
    registry = ShortcutRegistry(config=config)
    registry.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: calls.append("quit"), default_keys=(),
    ))
    registry.load()
    try:
        assert [str(key) for key in registry.keys_for("app.quit")] == [
            "Ctrl+Alt+Q",
        ]

        config.reset(SECTION, "app.quit")
        from PySide6.QtCore import QCoreApplication

        if QCoreApplication.instance() is not None:
            QCoreApplication.instance().processEvents()

        assert [str(key) for key in registry.keys_for("app.quit")] == ["Ctrl+Q"]
        assert registry.trigger("Ctrl+Q") == (None,)
        assert registry.trigger("Ctrl+Alt+Q") == ()
        assert calls == ["quit"]
    finally:
        registry.close()


def test_persist_writes_only_the_differences(
        config: Configue, tmp_path: Path,
) -> None:
    """``persist()`` 走配置器路径：差异写用户表，回到默认的删行。"""
    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    registry = ShortcutRegistry(config=config)
    # 随包表里的每条命令都登记槽：否则 persist() 会把「本进程没人认识」的命令
    # 当成「用户解绑」写回去（这是 ``_all_commands`` 刻意防的那件事）。
    for command in config.resolver.keys_of(SECTION):
        registry.register_slot(ShortcutSlot(id=command, handler=lambda: None))
    registry.load()
    try:
        # 把 app.quit 改回默认、把 window.close 改成别的
        registry.bind_command("app.quit", ["Ctrl+Q"])
        registry.bind_command("window.close", ["Ctrl+Alt+W"])

        path = registry.persist()

        assert path == config.base_dir / "shortcuts.ini"
        text = _user_text(tmp_path)
        assert "window.close" in text and "Ctrl+Alt+W" in text
        assert "app.quit" not in text
    finally:
        registry.close()
