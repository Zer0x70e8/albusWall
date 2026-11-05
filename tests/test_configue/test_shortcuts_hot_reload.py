#
"""快捷键热生效（缺口 2）：注册表订阅配置变更 + ``save_shortcut``。

三个层次：

* **纯逻辑**：没有 ``QApplication`` 时同步重建；``save_shortcut`` 走树视图；
* **合并**：同一个 tick 内多次变更只重建一次（需要 ``QApplication`` 的
  事件循环，用 offscreen 的 ``processEvents`` 推进）;
* **端到端**：``config.set`` / ``config.reset`` 之后，``trigger`` 真的按新
  键位触发（而不是只改了表）。

配置实例由 ``tests/test_configue/conftest.py`` 的隔离 fixture 提供。
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
)
from albuswall.core import Container

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot  # noqa: E402
from albuswall.ui.keymap.model import ShortcutBinding  # noqa: E402

from .conftest import write_ini  # noqa: E402


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


def _settle() -> None:
    """把延迟到「空闲点」的重建跑完；没有事件循环时什么也不做。

    注册表在**有** ``QCoreApplication`` 时用 ``QTimer.singleShot(0, ...)``
    合并同一 tick 的多次变更，所以断言 ``rebind_count`` 之前必须先推进一轮
    事件循环。测试对「有没有 app」不做假设（模块里创建一个 app 就会一直存在）。
    """
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is not None:
        app.processEvents()


@pytest.fixture
def config(tmp_path: Path):
    """portable 配置：随包默认表 + 空用户表。"""
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


@pytest.fixture
def registry(config: Configue):
    """绑好配置、登记了两个槽的注册表。"""
    calls: list[str] = []

    instance = ShortcutRegistry(config=config)
    instance.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: calls.append("quit"), help="Quit",
        default_keys=(),
    ))
    instance.register_slot(ShortcutSlot(
        id="window.close", handler=lambda: calls.append("close"),
        help="Close", default_keys=(),
    ))
    instance.load()
    instance.calls = calls          # type: ignore[attr-defined]
    try:
        yield instance
    finally:
        instance.close()


# ==================== 读：从配置器装载 ====================

def test_load_reads_the_configue_table(registry: ShortcutRegistry) -> None:
    assert registry.keys_for("app.quit") == tuple(
        registry.keys_for("app.quit"))            # 无异常即可
    assert [str(key) for key in registry.keys_for("app.quit")] == ["Ctrl+Q"]
    assert [str(key) for key in registry.keys_for("window.close")] == ["Ctrl+W"]


def test_unbound_command_stays_unbound(registry: ShortcutRegistry) -> None:
    """随包表里 ``= []`` 的命令：加载后仍然没有键位。"""
    assert registry.keys_for("window.album") == ()


# ==================== 热生效：set / reset 触发重建 ====================

def test_set_triggers_rebuild_and_new_key_works(
        registry: ShortcutRegistry, config: Configue,
) -> None:
    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))
    _settle()

    assert registry.rebind_count == 1
    assert [str(key) for key in registry.keys_for("app.quit")] == ["Ctrl+Alt+Q"]
    assert registry.trigger("Ctrl+Alt+Q") == (None,)
    assert registry.calls == ["quit"]


def test_rebuild_keeps_untouched_commands(
        registry: ShortcutRegistry, config: Configue,
) -> None:
    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))

    assert [str(key) for key in registry.keys_for("window.close")] == ["Ctrl+W"]


def test_change_in_another_section_is_ignored(
        registry: ShortcutRegistry, config: Configue,
) -> None:
    config.resolver.set("ui", "theme", "default")

    assert registry.rebind_count == 0


def test_one_tick_coalesces_several_changes(
        registry: ShortcutRegistry, config: Configue, qapp,
) -> None:
    """同一 tick 改三个命令：只重建一次。"""
    for command, keys in (
            ("app.quit", ("Ctrl+Alt+Q",)),
            ("window.close", ("Ctrl+Alt+W",)),
            ("window.album", ("Ctrl+Alt+1",)),
    ):
        config.resolver.set(SECTION, command, ShortcutRecord(keys=keys))

    # 事件循环还没跑：一次都没重建（延迟到空闲点）
    assert registry.rebind_count == 0

    qapp.processEvents()

    assert registry.rebind_count == 1
    assert [str(key) for key in registry.keys_for("window.album")] == ["Ctrl+Alt+1"]


def test_rebuild_without_qt_runs_synchronously(
        config: Configue, monkeypatch,
) -> None:
    """没有 ``QCoreApplication`` 时同步重建（否则「推迟」= 永远不执行）。"""
    from albuswall.ui.keymap import registry as registry_module

    monkeypatch.setattr(registry_module, "_qt_application", lambda: None)
    calls: list[str] = []
    instance = ShortcutRegistry(config=config)
    instance.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: calls.append("quit"), default_keys=(),
    ))
    instance.load()
    try:
        config.resolver.set(
            SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))
        assert instance.rebind_count == 1
        assert instance.trigger("Ctrl+Alt+Q") == (None,)
    finally:
        instance.close()


# ==================== 写：save_shortcut ====================

def test_save_shortcut_updates_the_user_table(
        registry: ShortcutRegistry, config: Configue, tmp_path: Path,
) -> None:
    bundled = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path
    assert bundled is not None
    before = bundled.read_bytes()

    registry.save_shortcut("app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))

    text = (tmp_path / "shortcuts.ini").read_text(encoding="utf-8")
    assert "app.quit" in text
    assert "Ctrl+Alt+Q" in text
    # 随包默认表只读：一个字节都不许变
    assert bundled.read_bytes() == before


def test_save_shortcut_rebinds_via_the_event(
        registry: ShortcutRegistry,
) -> None:
    """写回也会广播 ``set`` → 绑定表自动跟着更新（不需要手动 load）。"""
    registry.save_shortcut("app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))
    _settle()

    assert registry.rebind_count == 1
    assert [str(key) for key in registry.keys_for("app.quit")] == ["Ctrl+Alt+Q"]


def test_save_shortcut_for_a_runtime_new_key(
        registry: ShortcutRegistry, config: Configue, tmp_path: Path,
) -> None:
    """运行期新 key（默认表里没有这一行）也能写进用户表并落盘。"""
    registry.register_slot(ShortcutSlot(
        id="plugin.new", handler=lambda: None, default_keys=(),
    ))
    registry.save_shortcut("plugin.new", ShortcutRecord(keys=("Ctrl+Alt+N",)))

    text = (tmp_path / "shortcuts.ini").read_text(encoding="utf-8")
    assert "plugin.new" in text
    assert config.resolver.get_typed(SECTION, "plugin.new").keys == ("Ctrl+Alt+N",)


def test_save_shortcut_with_flush_false_batches(
        registry: ShortcutRegistry, config: Configue, tmp_path: Path,
) -> None:
    registry.save_shortcut(
        "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)), flush=False)
    registry.save_shortcut(
        "window.close", ShortcutRecord(keys=("Ctrl+Alt+W",)), flush=False)

    # 还没落盘：内存里有值，文件里没有
    assert not (tmp_path / "shortcuts.ini").exists()

    config.flush(file=SHORTCUT_USER_FILE)
    text = (tmp_path / "shortcuts.ini").read_text(encoding="utf-8")
    assert "Ctrl+Alt+Q" in text and "Ctrl+Alt+W" in text


def test_save_shortcut_without_config_raises() -> None:
    instance = ShortcutRegistry()

    with pytest.raises(RuntimeError, match="bind_config"):
        instance.save_shortcut("app.quit", ShortcutRecord(keys=("Ctrl+Q",)))


def test_save_shortcut_rejects_empty_command(config: Configue) -> None:
    instance = ShortcutRegistry(config=config)

    with pytest.raises(ValueError):
        instance.save_shortcut("  ", ShortcutRecord())


# ==================== 解绑 / 订阅生命周期 ====================

def test_bind_config_none_unsubscribes(
        registry: ShortcutRegistry, config: Configue,
) -> None:
    registry.bind_config(None)

    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))
    _settle()

    assert registry.rebind_count == 0


def test_close_unsubscribes(registry: ShortcutRegistry, config: Configue) -> None:
    registry.close()

    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))
    _settle()

    assert registry.rebind_count == 0


def test_config_events_survive_without_subscribers(config: Configue) -> None:
    """没有订阅者时写配置不能因为广播而报错。"""
    config.resolver.set(SECTION, "app.quit", ShortcutRecord(keys=("Ctrl+Q",)))


# ==================== 旧路径不受影响 ====================

def test_legacy_shortcut_config_still_works(tmp_path: Path) -> None:
    """没绑配置器时仍读独立的两份 ini（改造前的行为）。"""
    from albuswall.ui.keymap import ShortcutConfig

    defaults = tmp_path / "defaults.ini"
    defaults.write_text(
        '[shortcuts]\napp.quit = ["Ctrl+Q"]\n', encoding="utf-8")
    user = tmp_path / "user.ini"
    legacy = ShortcutConfig(defaults_path=defaults, user_path=user)

    instance = ShortcutRegistry()
    instance.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: None, default_keys=(),
    ))
    try:
        instance.load(legacy)
        assert [str(key) for key in instance.keys_for("app.quit")] == ["Ctrl+Q"]
    finally:
        instance.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
