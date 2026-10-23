#
"""窗口位置持久化（动态配置子树 window）的端到端验证。

被测对象：``albuswall.ui.builtin.presenter.window_presenter.WindowPresenter``

覆盖：

  1. 状态子树经 DYNAMIC_REGISTRY 懒加载（不再需要 setup_dynamic_config 自调用）
  2. 恢复：window.json 里的 x/y/width/height/maximized 应用到窗口
  3. 更新：Move / Resize / WindowStateChange → debounce 后写一次 window.json
  4. 退出：teardown 立即落盘（debounce 窗口内的改动不丢）
  5. 首次运行：window.json 缺失时用默认值，不抛异常

用 offscreen Qt 平台运行，不需要显示器：

    QT_QPA_PLATFORM=offscreen python test/test_window_state_persistence.py

装了 pytest 的话 ``pytest test/test_window_state_persistence.py`` 也能跑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from textwrap import dedent

# 强制离屏：桌面会话里可能已有 QT_QPA_PLATFORM=wayland;xcb，
# setdefault 会被它绕过 —— 测试就会真的弹窗口。
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget

# 静态配置必须先就绪：window_presenter 在 import 时就要读
# static.files.window_state 来声明 @dynamic_state。
_TMP = Path(tempfile.mkdtemp(prefix="albuswall-window-"))
_CACHE_DIR = _TMP / "cache"


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
        theme = {_TMP / "theme"}
        cache = {_CACHE_DIR}

        [ui]
        theme = default
        auto_complete_theme = false

        [files]
        qss = qss.qss
    """).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config


# WindowPresenterConfs 的字段必须在 ConfigDeclaration.load 之前登记，
# 否则 static.files.window_state 取不到。
import albuswall.ui.builtin.config  # noqa: E402,F401

CONFIG = _reset_config()

from albuswall.ui.builtin.presenter.window_presenter import (  # noqa: E402
    WindowPresenter, WindowState,
)

_app = QApplication.instance() or QApplication([])

#: @dynamic_state 计算出的落盘路径
STATE_PATH = _CACHE_DIR / str(CONFIG.static.files.window_state)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _pump(seconds: float = 0.0) -> None:
    """跑一下事件循环；seconds>0 时持续 seconds 秒。"""
    deadline = time.monotonic() + seconds
    while True:
        _app.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def _write_state(**values) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(values), encoding="utf-8")
    return STATE_PATH


def _read_state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _fresh_dynamic(config) -> None:
    """清掉 dynamic 上已缓存的 window 子树，逼它重新懒加载。"""
    config.dynamic.__dict__.pop("window", None)


def _make_presenter() -> tuple[QWidget, WindowPresenter]:
    _pump()
    window = QWidget()
    presenter = WindowPresenter(window, window)
    presenter._save_timer.setInterval(40)  # 缩短 debounce，测试别等太久
    return window, presenter


# ══════════════════════════════════════════════════════════════
# 懒加载
# ══════════════════════════════════════════════════════════════
def test_state_is_lazily_loaded_from_registry() -> None:
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()

    assert "window" not in CONFIG.dynamic.__dict__

    state = presenter._load_state()

    assert isinstance(state, WindowState)
    assert CONFIG.dynamic.__dict__["window"] is state


# ══════════════════════════════════════════════════════════════
# 恢复
# ══════════════════════════════════════════════════════════════
def test_restore_applies_saved_geometry() -> None:
    _write_state(x=40, y=50, width=640, height=480, maximized=False)
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()

    presenter._restore_window_state()

    g = window.geometry()
    assert (g.x(), g.y(), g.width(), g.height()) == (40, 50, 640, 480)
    assert not presenter._save_timer.isActive(), "恢复动作不该触发回写"

    window.deleteLater()
    _pump()


def test_first_run_uses_defaults_and_creates_state_file() -> None:
    """window.json 缺失：不抛异常，落到 WindowState 的默认几何，再写回一次。"""
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()
    window.resize(320, 240)

    presenter._restore_window_state()  # 不抛即通过

    # 默认值来自 WindowState.width/height
    assert (window.width(), window.height()) == (800, 600)

    presenter._persist_window_state()
    assert STATE_PATH.is_file()
    saved = _read_state()
    assert (saved["width"], saved["height"]) == (800, 600)

    window.deleteLater()
    _pump()


# ══════════════════════════════════════════════════════════════
# debounce 回写
# ══════════════════════════════════════════════════════════════
def test_geometry_events_are_debounced_into_one_write() -> None:
    _write_state(x=0, y=0, width=800, height=600, maximized=False)
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()
    presenter._restore_window_state()
    presenter._install_geometry_tracking()

    writes: list[Path] = []
    original_save = WindowState.save

    def counting_save(self, path=None):
        writes.append(Path(path) if path else Path(self._ctx.path))
        return original_save(self, path)

    WindowState.save = counting_save
    try:
        window.move(11, 22)
        window.resize(900, 550)
        for _ in range(5):
            presenter.eventFilter(window, QEvent(QEvent.Type.Move))
            _pump(0.01)
        assert presenter._save_timer.isActive(), "事件应重置 debounce 计时器"
        assert writes == [], "debounce 期间不该落盘"

        _pump(0.4)  # 等计时器到期
    finally:
        WindowState.save = original_save
        presenter.teardown()

    assert len(writes) == 1, f"应合并为一次写入，实际 {len(writes)}"
    saved = _read_state()
    assert saved["x"] == 11 and saved["y"] == 22
    assert saved["width"] == 900 and saved["height"] == 550

    window.deleteLater()
    _pump()


def test_teardown_flushes_pending_change() -> None:
    _write_state(x=0, y=0, width=800, height=600, maximized=False)
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()
    presenter._restore_window_state()
    presenter._install_geometry_tracking()

    window.move(7, 8)
    window.resize(700, 500)
    presenter.eventFilter(window, QEvent(QEvent.Type.Resize))
    assert presenter._save_timer.isActive()
    assert _read_state()["x"] == 0  # 还没落盘

    presenter.teardown()  # 退出前立即写

    assert not presenter._save_timer.isActive()
    saved = _read_state()
    assert (saved["x"], saved["y"]) == (7, 8)
    assert (saved["width"], saved["height"]) == (700, 500)

    window.deleteLater()
    _pump()


def test_restore_does_not_track_itself() -> None:
    """_restoring 期间产生的 move/resize 不应排队回写。"""
    _write_state(x=40, y=50, width=640, height=480, maximized=False)
    _fresh_dynamic(CONFIG)
    window, presenter = _make_presenter()
    presenter._install_geometry_tracking()

    presenter._restore_window_state()
    # 恢复期间的事件被 _restoring 挡掉，结束时的 stop() 清掉残余计时器
    assert not presenter._save_timer.isActive()

    _pump(0.2)
    # 即便之后还有排队的事件触发了一次回写，写回的也是同一份几何
    saved = _read_state()
    assert (saved["x"], saved["y"]) == (40, 50)
    assert (saved["width"], saved["height"]) == (640, 480)

    window.deleteLater()
    _pump()


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
