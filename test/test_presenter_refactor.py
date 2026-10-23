#
"""presenter 层回归：三个装配 bug 的守护测试。

覆盖：

1. ``IngestSourcePresenter`` 在**没有** trash 服务时也必须接上
   ``source_service.scan_finished``（旧实现里两个服务缺一个就整体 return，
   于是扫描完成回调永远不触发）；
2. ``teardown()`` 之后两条后端订阅都要断干净（幂等）；
3. ``PresenterManager._close_viewer`` 关闭详情时只查一次 grid 选中
   （旧实现对 ``current_asset_uuid`` getter 调用了两次）；
4. ``WindowPresenter.setup`` 只恢复一次窗口几何（旧实现调了两次，
   第二次会把第一次的恢复动作又走一遍）。

不依赖 pytest：``python test/test_presenter_refactor.py`` 直接运行，
用 offscreen Qt 平台，不需要显示器。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

_app = QApplication.instance() or QApplication([])


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class FakeSignal:
    """够用的信号替身：记住 slot，可 emit / disconnect。"""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot) -> None:
        self.slots.remove(slot)

    def emit(self, *args, **kwargs) -> None:
        for slot in list(self.slots):
            slot(*args, **kwargs)


class FakeSourceService:
    def __init__(self) -> None:
        self.scan_finished = FakeSignal()

    def list_sources(self) -> list:
        return []

    def get_source(self, source_id):  # noqa: ARG002
        return None


class FakeTrashService:
    def __init__(self) -> None:
        self.sources_changed = FakeSignal()


class FakeDetailWidget:
    def set_form_data(self, form) -> None: ...
    def get_form_data(self): return None
    def clear(self) -> None: ...


class FakeSourceView(QObject):
    card_clicked = Signal(object)
    detail_closed = Signal()
    context_menu_requested = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.view = QWidget()
        self.add_button = QPushButton()
        self.apply_button = QPushButton()
        self.close_button = QPushButton()
        self.detail_widget = FakeDetailWidget()
        self.model = None

    def set_model(self, model, delegate) -> None:
        self.model = model


def make_ingest_presenter(trash=None):
    from albuswall.ui.builtin.presenter.source_presenter import (
        IngestSourcePresenter,
    )

    service = FakeSourceService()
    presenter = IngestSourcePresenter(FakeSourceView(), service, trash)
    return presenter, service


# ══════════════════════════════════════════════════════════════
# 1~2. 后端订阅：缺 trash 也要接上 scan_finished
# ══════════════════════════════════════════════════════════════
def test_scan_finished_bound_without_trash_service() -> None:
    presenter, service = make_ingest_presenter(trash=None)

    assert presenter._scan_bound is True, "scan_finished 必须被订阅"
    assert service.scan_finished.slots == [presenter._on_scan_finished_backend]

    # 后端线程回调 → 事件安全地搬回主线程（不抛异常即可）
    service.scan_finished.emit(None, event=SimpleNamespace(skipped=True))
    _app.processEvents()

    presenter.teardown()


def test_teardown_unbinds_both_services() -> None:
    trash = FakeTrashService()
    presenter, service = make_ingest_presenter(trash=trash)

    assert presenter._scan_bound is True
    assert presenter._trash_bound is True

    presenter.teardown()
    assert service.scan_finished.slots == []
    assert trash.sources_changed.slots == []
    assert presenter._scan_bound is False
    assert presenter._trash_bound is False

    presenter.teardown()  # 幂等
    assert service.scan_finished.slots == []


def test_trash_change_triggers_reload() -> None:
    trash = FakeTrashService()
    presenter, _service = make_ingest_presenter(trash=trash)

    calls: list[int] = []
    presenter.reload = lambda: calls.append(1)  # type: ignore[method-assign]

    trash.sources_changed.emit(None, source_id=1, kind="soft_delete")
    _app.processEvents()

    assert calls == [1], "sources_changed 应搬到主线程后 reload 一次"
    presenter.teardown()


# ══════════════════════════════════════════════════════════════
# 3. 关闭详情时只查一次 grid 选中
# ══════════════════════════════════════════════════════════════
def test_close_viewer_queries_grid_selection_once() -> None:
    from albuswall.ui.builtin.presenter.manager import PresenterManager

    mgr = PresenterManager(None, None)  # type: ignore[arg-type]
    mgr._window = SimpleNamespace(  # noqa: SLF001 - 只测 _close_viewer
        detail=SimpleNamespace(refresh=lambda: None),
    )

    lookups: list[int] = []

    class FakeThumb:
        def current_asset_uuid(self) -> str:
            lookups.append(1)
            return "u-1"

    mgr.thumb_presenter = FakeThumb()  # type: ignore[assignment]
    mgr._detail_visible = True  # noqa: SLF001

    mgr._close_viewer()  # noqa: SLF001

    assert lookups == [1], f"getter 只该调一次，实际 {len(lookups)} 次"
    assert mgr._active_asset_uuid == "u-1"  # noqa: SLF001
    assert mgr._detail_visible is False  # noqa: SLF001


def test_close_viewer_without_grid_clears_asset() -> None:
    from albuswall.ui.builtin.presenter.manager import PresenterManager

    mgr = PresenterManager(None, None)  # type: ignore[arg-type]
    mgr._window = SimpleNamespace(  # noqa: SLF001
        detail=SimpleNamespace(refresh=lambda: None),
    )
    mgr._active_asset_uuid = "stale"  # noqa: SLF001

    mgr._close_viewer()  # noqa: SLF001

    assert mgr._active_asset_uuid is None  # noqa: SLF001


# ══════════════════════════════════════════════════════════════
# 4. 窗口几何只恢复一次
# ══════════════════════════════════════════════════════════════
class _FakeContainer:
    """只提供 setup 需要的 config（关掉主题补全，避免碰文件系统）。"""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            static=SimpleNamespace(
                ui=SimpleNamespace(auto_complete_theme=False),
                path=SimpleNamespace(icon=None),
            ),
        )

    def require(self, name: str):
        assert name == "config"
        return self.config


def _ensure_static_config():
    """``window_presenter`` 在 import 时就要读 static.files.window_state。

    与 test_window_state_persistence.py 同款最小引导：只把静态配置拉起来，
    不碰用户真实配置目录。
    """
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

    tmp = Path(tempfile.mkdtemp(prefix="albuswall-presenter-"))
    main = tmp / "main.ini"
    main.write_text(dedent(f"""
        [path]
        theme = {tmp / "theme"}
        cache = {tmp / "cache"}

        [ui]
        theme = default
        auto_complete_theme = false

        [files]
        qss = qss.qss
    """).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config


# WindowPresenterConfs 的字段必须在 ConfigDeclaration.load 之前登记
import albuswall.ui.builtin.config  # noqa: E402,F401

_CONFIG = _ensure_static_config()


def test_setup_restores_window_state_once() -> None:
    from albuswall.ui.builtin.presenter.window_presenter import WindowPresenter

    window = QWidget()
    presenter = WindowPresenter(window, window)
    # 本机可能被判定为平铺桌面（_manage_geometry=False）；这里强制打开
    # 几何管理，只为验证「恢复一次」的调用次数契约。
    presenter._manage_geometry = True  # noqa: SLF001

    restores: list[int] = []
    presenter._restore_window_state = lambda: restores.append(1)  # noqa: SLF001
    presenter._wire_navigation = lambda: None  # noqa: SLF001
    presenter._apply_style_sheet = lambda: None  # noqa: SLF001

    presenter.setup(_FakeContainer())

    assert restores == [1], f"应只恢复一次，实际 {len(restores)} 次"

    presenter.teardown()
    window.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 运行器
# ══════════════════════════════════════════════════════════════
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
