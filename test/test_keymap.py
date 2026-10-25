#
"""按键映射（keymap）的端到端验证。

覆盖：

  1. ``dynamic.keymap`` 子配置文件经 DYNAMIC_REGISTRY 懒加载，
     落点是 ``path.config`` + ``[files] keymap``；
  2. 按键文本规范化（PortableText / 单段限制）；
  3. 动作登记表：重复 id、replace、排序；
  4. store：默认值 / 覆盖 / 显式解绑 / 恢复默认 / 冲突检测；
  5. KeymapManager：命中动作、``when`` 条件、输入框策略、总开关；
  6. 设置页：列出全部「动作 ↔ 按键」，搜索过滤，编辑后立即写回。

用 offscreen Qt 平台运行，不需要显示器：

    QT_QPA_PLATFORM=offscreen python test/test_keymap.py

装了 pytest 的话 ``pytest test/test_keymap.py`` 也能跑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

# 强制离屏：桌面会话里可能已有 QT_QPA_PLATFORM，setdefault 会被绕过。
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

# 静态配置必须先就绪：keymap 的动作声明 / 文件名声明要在 load 之前登记。
_TMP = Path(tempfile.mkdtemp(prefix="albuswall-keymap-"))
_CONFIG_DIR = _TMP / "config"


def _reset_config():
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

    main = _TMP / "main.ini"
    main.write_text(dedent(f"""
        [path]
        config = {_CONFIG_DIR}
        data = {_TMP / "data"}
        cache = {_TMP / "cache"}
        temp = {_TMP / "temp"}
        theme = {_TMP / "theme"}

        [ui]
        theme = default
        auto_complete_theme = false

        [files]
        qss = qss.qss
        keymap = keymap.json
    """).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config


# WindowPresenterConfs / 键位文件名声明必须在 ConfigDeclaration.load 之前登记。
import albuswall.ui.builtin.config  # noqa: E402,F401

CONFIG = _reset_config()

from albuswall.ui.builtin.keymap import (  # noqa: E402
    KeyAction,
    get_action,
    iter_actions,
    register_action,
    unregister_action,
)
from albuswall.ui.builtin.keymap import store  # noqa: E402
from albuswall.ui.builtin.keymap.actions import (  # noqa: E402
    ensure_builtin_actions,
)
from albuswall.ui.builtin.keymap.keys import (  # noqa: E402
    display_text,
    normalize_sequence,
)
from albuswall.ui.builtin.keymap.manager import (  # noqa: E402
    KeymapManager,
)

_app = QApplication.instance() or QApplication([])

KEYMAP_FILE = _CONFIG_DIR / "keymap.json"


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _prepare() -> None:
    """把全局 Configue 重新指向本文件的临时目录。

    ``Configue`` 是进程级单例，其它测试模块也会重建它的 resolver；pytest
    收集顺序不受本文件控制，所以每个用例开头都显式重置一次，保证键位
    落到 ``_CONFIG_DIR`` 而不是别人的临时目录 / 用户真实配置目录。
    """
    _reset_config()
    CONFIG.dynamic.__dict__.pop("keymap", None)
    ensure_builtin_actions()


def _fresh_state() -> None:
    """丢掉已缓存的 dynamic.keymap，逼它从磁盘重新加载。"""
    CONFIG.dynamic.__dict__.pop("keymap", None)


def _write_keymap(payload: dict) -> None:
    KEYMAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYMAP_FILE.write_text(json.dumps(payload), encoding="utf-8")
    _fresh_state()


def _read_keymap() -> dict:
    return json.loads(KEYMAP_FILE.read_text(encoding="utf-8"))


def _clear_keymap() -> None:
    if KEYMAP_FILE.exists():
        KEYMAP_FILE.unlink()
    _fresh_state()


def _key_event(key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


class _FakeWindow(QWidget):
    """够用的主窗口替身：overlay / 各浮层 / setting 页都在。"""

    def __init__(self) -> None:
        super().__init__()
        self.setting = QWidget(self)
        self.source = QWidget(self)
        self.detail = QWidget(self)
        self.title_bar = QWidget(self)
        self.overlay = _FakeOverlay(self)
        self.closed = False
        self.minimized = False

    def close(self) -> None:  # noqa: N802 - Qt 命名
        self.closed = True

    def showMinimized(self) -> None:  # noqa: N802 - Qt 命名
        self.minimized = True


class _FakeOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._current: QWidget | None = None

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        self._current = widget

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        return self._current

    def is_blank(self) -> bool:
        return self._current is None

    def show_blank(self) -> None:
        self._current = None


# ══════════════════════════════════════════════════════════════
# 1. 动态子树 / 子配置文件
# ══════════════════════════════════════════════════════════════
def test_keymap_is_registered_dynamic_subtree() -> None:
    _prepare()
    from albuswall.configue.dynamic_declaration import DYNAMIC_REGISTRY

    spec = DYNAMIC_REGISTRY.get("keymap")
    assert spec is not None, "keymap 未登记为动态子树"
    assert spec.base_key == ("path", "config")
    assert spec.autosave is True

    _fresh_state()
    path = store.keymap_file()
    assert path == _CONFIG_DIR / "keymap.json", path
    assert store.keymap_state().path == path


def test_bindings_are_persisted_into_the_sub_config_file() -> None:
    _prepare()
    _clear_keymap()
    store.set_binding("viewer.next", "Ctrl+Right")

    assert KEYMAP_FILE.is_file()
    payload = _read_keymap()
    assert payload["bindings"]["viewer.next"] == "Ctrl+Right"

    # 重新从磁盘加载后仍然生效
    _fresh_state()
    assert store.binding_of("viewer.next") == "Ctrl+Right"


# ══════════════════════════════════════════════════════════════
# 2. 按键文本
# ══════════════════════════════════════════════════════════════
def test_normalize_sequence_canonicalizes() -> None:
    _prepare()
    assert normalize_sequence("ctrl+,") == "Ctrl+,"
    assert normalize_sequence(" ALT + left ") == "Alt+Left"
    assert normalize_sequence("f1") == "F1"
    assert normalize_sequence("") == ""
    assert normalize_sequence(None) == ""

    assert display_text("Ctrl+,")  # 具体平台文本，只要求非空
    assert normalize_sequence(display_text("Ctrl+,")) == "Ctrl+,"


def test_normalize_sequence_rejects_multi_key_sequences() -> None:
    _prepare()
    try:
        normalize_sequence("Ctrl+K, Ctrl+C")
    except ValueError as exc:
        assert "single" in str(exc).lower() or "multi" in str(exc).lower()
    else:
        raise AssertionError("多段序列应当被拒绝")


# ══════════════════════════════════════════════════════════════
# 3. 动作登记表
# ══════════════════════════════════════════════════════════════
def test_registry_rejects_duplicates_and_supports_replace() -> None:
    _prepare()
    action = KeyAction(id="test.registry", label="Test action")
    register_action(action)
    try:
        try:
            register_action(KeyAction(id="test.registry", label="dup"))
        except ValueError as exc:
            assert "duplicate" in str(exc)
        else:
            raise AssertionError("重复 id 应当抛错")

        replacement = KeyAction(
            id="test.registry", label="Replaced", default_keys="Ctrl+Alt+R",
        )
        register_action(replacement, replace=True)
        assert get_action("test.registry") is replacement
    finally:
        unregister_action("test.registry")
    assert get_action("test.registry") is None


def test_builtin_actions_are_registered_and_ordered() -> None:
    _prepare()
    ensure_builtin_actions()
    ids = [action.id for action in iter_actions()]
    for expected in (
            "window.settings",
            "window.sources",
            "window.back",
            "window.quit",
            "search.focus",
            "viewer.next",
            "viewer.previous",
            "viewer.favorite",
    ):
        assert expected in ids, expected

    ranks = [action.sort_key for action in iter_actions()]
    assert ranks == sorted(ranks), "动作应按分组 / order / id 排序"


# ══════════════════════════════════════════════════════════════
# 4. store
# ══════════════════════════════════════════════════════════════
def test_store_defaults_overrides_and_reset() -> None:
    _prepare()
    ensure_builtin_actions()
    _clear_keymap()

    assert store.binding_of("viewer.next") == "Right"
    assert store.binding_of("window.minimize") == ""  # 默认不绑定

    store.set_binding("viewer.next", "ctrl+right")
    assert store.binding_of("viewer.next") == "Ctrl+Right"
    assert store.overrides() == {"viewer.next": "Ctrl+Right"}

    # 显式解绑：空串覆盖默认值
    store.set_binding("viewer.next", "")
    assert store.binding_of("viewer.next") == ""
    assert store.overrides() == {"viewer.next": ""}

    store.reset_binding("viewer.next")
    assert store.binding_of("viewer.next") == "Right"
    assert store.overrides() == {}

    # 与默认值相同的写入不会留下覆盖
    store.set_binding("viewer.next", "Right")
    assert store.overrides() == {}


def test_store_enabled_toggle_and_conflicts() -> None:
    _prepare()
    ensure_builtin_actions()
    _clear_keymap()

    assert store.is_enabled() is True
    store.set_enabled(False)
    assert store.is_enabled() is False
    assert _read_keymap()["enabled"] is False
    store.set_enabled(True)

    store.set_binding("viewer.favorite", "Ctrl+F")
    conflicts = store.conflicts()
    assert "Ctrl+F" in conflicts
    assert set(conflicts["Ctrl+F"]) == {"search.focus", "viewer.favorite"}

    store.reset_all()
    assert store.conflicts() == {}


# ══════════════════════════════════════════════════════════════
# 5. manager
# ══════════════════════════════════════════════════════════════
def test_manager_dispatches_bound_keys() -> None:
    _prepare()
    ensure_builtin_actions()
    _clear_keymap()
    window = _FakeWindow()
    manager = KeymapManager(window, parent=window)
    manager.setup()
    try:
        assert manager.handle_key_event(
            _key_event(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier),
            focus=window,
        )
        assert window.overlay.currentWidget() is window.setting

        assert manager.handle_key_event(
            _key_event(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier),
            focus=window,
        )
        assert window.closed

        # 未被占用的键不消费
        assert not manager.handle_key_event(
            _key_event(Qt.Key.Key_F12), focus=window,
        )
    finally:
        manager.teardown()
        window.deleteLater()


def test_manager_respects_when_predicate_and_enabled_switch() -> None:
    _prepare()
    ensure_builtin_actions()
    _clear_keymap()

    calls: list[str] = []
    window = _FakeWindow()
    window.detail_open = False
    register_action(KeyAction(
        id="test.gated",
        label="Gated action",
        default_keys="Ctrl+Alt+G",
        handler=lambda ctx: calls.append("gated"),
        when=lambda ctx: bool(getattr(ctx.window, "detail_open", False)),
    ))
    manager = KeymapManager(window, parent=window)
    manager.setup()
    event = _key_event(
        Qt.Key.Key_G,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
    )
    try:
        assert not manager.handle_key_event(event, focus=window)
        assert calls == []

        window.detail_open = True
        assert manager.handle_key_event(event, focus=window)
        assert calls == ["gated"]

        store.set_enabled(False)
        assert not manager.handle_key_event(event, focus=window)
        assert calls == ["gated"]
    finally:
        store.set_enabled(True)
        manager.teardown()
        unregister_action("test.gated")
        window.deleteLater()


def test_manager_ignores_plain_keys_inside_text_inputs() -> None:
    _prepare()
    ensure_builtin_actions()
    _clear_keymap()

    from PySide6.QtWidgets import QLineEdit

    window = _FakeWindow()
    edit = QLineEdit(window)
    calls: list[str] = []
    register_action(KeyAction(
        id="test.plain",
        label="Plain key",
        default_keys="F2",
        handler=lambda ctx: calls.append("plain"),
    ))
    manager = KeymapManager(window, parent=window)
    manager.setup()
    try:
        event = _key_event(Qt.Key.Key_F2)
        # 焦点在输入框：裸键不抢
        assert not manager.handle_key_event(event, focus=edit)
        assert calls == []
        # 焦点在主窗口：正常触发
        assert manager.handle_key_event(event, focus=window)
        assert calls == ["plain"]
    finally:
        manager.teardown()
        unregister_action("test.plain")
        window.deleteLater()


def test_manager_yields_while_capturing_a_key() -> None:
    """在设置页采键（QKeySequenceEdit）时，任何组合键都应先给编辑框。"""
    _prepare()
    from PySide6.QtWidgets import QKeySequenceEdit

    ensure_builtin_actions()
    _clear_keymap()

    window = _FakeWindow()
    capture = QKeySequenceEdit(window)
    manager = KeymapManager(window, parent=window)
    manager.setup()
    try:
        event = _key_event(
            Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier,
        )
        assert not manager.handle_key_event(event, focus=capture)
        assert not window.closed
        # 同样的组合键在主窗口焦点下正常触发
        assert manager.handle_key_event(event, focus=window)
        assert window.closed
    finally:
        manager.teardown()
        window.deleteLater()


# ══════════════════════════════════════════════════════════════
# 6. 设置页
# ══════════════════════════════════════════════════════════════
def test_settings_page_lists_search_and_edits_bindings() -> None:
    _prepare()
    from albuswall.ui.builtin.keymap.page import KeymapPage

    ensure_builtin_actions()
    _clear_keymap()

    page = KeymapPage()
    try:
        assert len(page._rows) >= len(list(iter_actions()))
        assert page.enabled_box.isChecked()

        # 搜索：动作名 / id / 当前按键都能命中
        for query, expected_id in (
                ("next", "viewer.next"),
                ("viewer.favorite", "viewer.favorite"),
                ("ctrl+f", "search.focus"),
        ):
            tokens = query.casefold().split()
            matched = [row._binding.id for row in page._rows if row.contains(tokens)]
            assert expected_id in matched, (query, matched)

        page.search.setText("zzz-no-match")
        assert not any(
            row.contains(["zzz-no-match"]) for row in page._rows
        )
        page.search.setText("")

        # 编辑一行 → 立即写回子配置文件
        row = next(r for r in page._rows if r._binding.id == "viewer.next")
        row.editor.setKeySequence(QKeySequence("Ctrl+Right"))
        row._on_commit()
        assert store.binding_of("viewer.next") == "Ctrl+Right"
        assert _read_keymap()["bindings"]["viewer.next"] == "Ctrl+Right"

        # 恢复默认
        row._on_reset()
        assert store.binding_of("viewer.next") == "Right"

        # 总开关
        page.enabled_box.setChecked(False)
        assert store.is_enabled() is False
        page.enabled_box.setChecked(True)
    finally:
        page.deleteLater()


def test_settings_page_shows_leftover_unknown_bindings() -> None:
    _prepare()
    from albuswall.ui.builtin.keymap.page import KeymapPage

    ensure_builtin_actions()
    _write_keymap({"bindings": {"legacy.action": "Ctrl+Alt+L"}})

    page = KeymapPage()
    try:
        unknown = next(
            (row for row in page._rows
             if row._binding.id == "legacy.action"),
            None,
        )
        assert unknown is not None, "残留的未知动作应该出现在列表里"
        assert unknown._binding.is_known is False
        assert unknown.editor.isEnabled() is False

        unknown._on_reset()
        assert "legacy.action" not in store.overrides()
    finally:
        page.deleteLater()
        _clear_keymap()


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
