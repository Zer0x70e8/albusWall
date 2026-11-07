#
"""设置页被动视图的**独立演示**。

**真的部分**（与生产同一条路）：dump → ``SettingTree`` 归一化 →
``SettingLayout`` 再处理 → ``SettingView`` 渲染 → ``collect_values()`` →
``write_back_result()`` 报告形状。

**假的部分**（只有 IO）：``DemoSettingsPresenter`` 冒充配置器——它不读写
磁盘，只把值记进 :class:`~albuswall.ui.setting.demo_data.DemoState`，并按
``dry_run`` / ``collect_errors`` / ``flush=False`` 三种模式模拟写回；外部
set / remove / reset 与**子线程**回调也由它发（视图内部用 Qt 信号排队）。

运行::

    QT_QPA_PLATFORM=offscreen python -m albuswall.ui.setting.demo --smoke
    python -m albuswall.ui.setting.demo
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .contract import Scope, SubmitMode, format_value
from .demo_data import (
    DemoState,
    build_demo_dump,
    leaf_index,
)
from .dialogs import AddComponentDialog, MoveItemDialog
from .layout import SettingLayout, apply_layout, builtin_layout, merge_layouts
from .result import write_back_result
from .view import SettingView

__all__ = ["DemoSettingsPresenter", "SettingsDemoWindow", "main"]

_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class DemoSettingsPresenter(QObject):
    """特制的 presenter / controller：假后端 + 三种提交模式 + 外部通知。"""

    status_changed = Signal(str)

    def __init__(
            self,
            view: SettingView,
            *,
            state: DemoState | None = None,
            parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.view = view
        self.state = state or DemoState()
        self.last_result: dict[str, Any] | None = None
        self._last_values: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []
        self.layout = SettingLayout()

        view.submit_requested.connect(self._on_submit_requested)
        view.retry_requested.connect(self._retry)
        view.reload_requested.connect(self.reload_from_model)
        view.move_requested.connect(self._on_move_requested)
        view.reset_move_requested.connect(self._on_reset_move_requested)
        view.add_component_requested.connect(self._on_add_component_requested)
        self.reload_from_model(announce=False)

    # ------------------------------------------------------------------
    # dump 供给
    # ------------------------------------------------------------------

    def build_dump(self) -> dict[str, Any]:
        layout = merge_layouts(builtin_layout(), self.layout)
        return apply_layout(build_demo_dump(self.state), layout)

    def reload_from_model(self, *, announce: bool = True) -> None:
        """模拟「重新 dump」：把最新假后端状态交给视图。"""
        self.view.set_dump_schema(self.build_dump())
        if announce:
            self.status_changed.emit("已重新 dump（未提交的本地改动被丢弃）")

    # ------------------------------------------------------------------
    # 外部通知（set / remove / reset / 子线程）
    # ------------------------------------------------------------------

    def external_set(self, key: str, value: Any) -> None:
        leaf = _leaf(key)
        self.state.values[key] = deepcopy(value)
        self.state.dirty.discard(key)
        self.state.pending.discard(key)
        node = (str(leaf["section"]), str(leaf["config_key"]))
        self.view.on_node_changed(node, value, "set")
        self.status_changed.emit(f"外部通知：{key} = {format_value(value)}（set）")

    def external_remove(self, key: str) -> None:
        leaf = _leaf(key)
        default = deepcopy(leaf["default"])
        self.state.values.pop(key, None)
        self.state.dirty.discard(key)
        self.state.pending.discard(key)
        node = (str(leaf["section"]), str(leaf["config_key"]))
        self.view.on_node_changed(node, default, "remove")
        self.status_changed.emit(f"外部通知：{key} 已移除，回落到默认值（remove）")

    def external_reset(self, key: str) -> None:
        leaf = _leaf(key)
        default = deepcopy(leaf["default"])
        self.state.values.pop(key, None)
        self.state.dirty.discard(key)
        self.state.pending.discard(key)
        node = (str(leaf["section"]), str(leaf["config_key"]))
        self.view.on_node_changed(node, default, "reset")
        self.status_changed.emit(f"外部通知：{key} 重置为默认值（reset）")

    def threaded_set(self, key: str, value: Any) -> None:
        """从**子线程**调 ``view.on_node_changed``（验证线程边界契约）。"""
        leaf = _leaf(key)
        node = (str(leaf["section"]), str(leaf["config_key"]))
        self.state.values[key] = deepcopy(value)
        self.state.dirty.discard(key)
        self.state.pending.discard(key)

        def worker() -> None:
            try:
                self.view.on_node_changed(node, deepcopy(value), "set")
            except Exception as exc:  # noqa: BLE001  演示里把异常带到状态栏
                self.status_changed.emit(f"子线程通知失败：{exc}")
                return
            self.status_changed.emit(
                f"子线程通知：{key} = {format_value(value)}（set，已排队到主线程）",
            )

        thread = threading.Thread(
            target=worker, name="demo-setting-notify", daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def set_simulate_pending(self, enabled: bool) -> None:
        self.state.simulate_pending = bool(enabled)
        self.status_changed.emit(
            "已开启「模拟写盘失败」：下次保存会留下 pending"
            if enabled else "已关闭「模拟写盘失败」",
        )

    # ------------------------------------------------------------------
    # 布局再处理（演示：与正式 presenter 共用 layout / dialogs）
    # ------------------------------------------------------------------
    def _group_options(self, *, exclude: str | None = None) -> list[tuple[str, str]]:
        tree = self.view.dump
        if tree is None:
            return []
        return [
            (key, title)
            for key, title in tree.group_titles().items()
            if not key.startswith("__") and key != exclude
        ]

    def _leaf_group(self, key: str) -> str | None:
        tree = self.view.dump
        if tree is None:
            return None
        group = tree.group_of(key)
        return group["key"] if group is not None else None

    def _on_move_requested(self, key: str) -> None:
        dialog = MoveItemDialog(
            key, self._group_options(exclude=self._leaf_group(key)),
            parent=self.view,
        )
        if dialog.exec() != MoveItemDialog.DialogCode.Accepted:
            return
        group, title, description, group_order, order = dialog.target()
        self.layout.set_move(
            key, group, order=order,
            group_title=title,
            group_description=description,
            group_order=group_order,
        )
        self.reload_from_model(announce=False)
        self.view.set_scope(Scope.all())
        self.view.select_anchor(key)
        self.status_changed.emit(f"演示布局：{key} → {group}")

    def _on_reset_move_requested(self, key: str) -> None:
        if self.layout.remove_move(key):
            self.reload_from_model(announce=False)
            self.view.set_scope(Scope.all())
            self.view.select_anchor(key)
            self.status_changed.emit(f"演示布局：{key} 恢复默认位置")

    def _on_add_component_requested(self) -> None:
        dialog = AddComponentDialog(parent=self.view)
        if dialog.exec() != AddComponentDialog.DialogCode.Accepted:
            return
        spec = dialog.spec()
        self.layout.add_component_group(
            spec.key,
            title=spec.title,
            description=spec.description,
            order=spec.order,
            label=spec.components[0].text if spec.components else "",
        )
        self.reload_from_model(announce=False)
        self.view.set_scope(Scope.all())
        self.view.select_anchor(spec.key)
        self.status_changed.emit(f"演示组件分组：{spec.title}")

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def _on_submit_requested(self, mode_text: str, values: Any) -> None:
        self.submit(SubmitMode(mode_text), dict(values))

    def submit(
            self, mode: SubmitMode, values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """模拟写回层：返回与 ``SettingWriteResult.to_dict()`` 同形的结果。"""
        values = {key: deepcopy(value) for key, value in values.items()}
        self._last_values = values
        changes = self._changes(values)
        errors = self._validate(values)
        changed = [change["key"] for change in changes if change["changed"]]
        nodes = [list(self.state.node_of(key)) for key in changed]
        by_file = self._by_file(changed)

        if mode is SubmitMode.DRY_RUN:
            result = write_back_result(
                by_file=by_file, changes=changes, dry_run=True,
                validation_errors=errors, nodes=nodes, ok=not errors,
            )
        elif errors:
            result = write_back_result(
                by_file=by_file, changes=changes, validation_errors=errors,
                nodes=nodes, ok=False, error="validation failed",
            )
        elif mode is SubmitMode.DRAFT:
            self._store(values, changed, pending=True)
            pending = [
                f"[{_leaf(key)['section']}]{_leaf(key)['config_key']}"
                for key in changed
            ]
            result = write_back_result(
                by_file=by_file, changes=changes, applied=changed,
                nodes=nodes, deferred=True, pending=pending, ok=False,
            )
        elif self.state.simulate_pending and changed:
            self._store(values, changed, pending=True)
            leaf = _leaf(changed[0])
            pending = [f"[{leaf['section']}]{leaf['config_key']}"]
            result = write_back_result(
                by_file=by_file, changes=changes, applied=changed,
                nodes=nodes, pending=pending, ok=False,
                error="模拟写盘失败：Permission denied",
            )
        else:
            self._store(values, changed, pending=False)
            result = write_back_result(
                written=sorted(by_file), by_file=by_file, flushed=changed,
                applied=changed, changes=changes, nodes=nodes, ok=True,
            )

        self.last_result = result
        self.view.show_submit_result(result)
        if result["ok"]:
            self.status_changed.emit(
                f"保存成功：{len(changed)} 项变更 / {len(by_file)} 个文件",
            )
        elif result["validation_errors"]:
            self.status_changed.emit(
                f"校验未通过：{len(result['validation_errors'])} 项（错误已按 key 回填到行内）",
            )
        elif result["pending"]:
            self.status_changed.emit(
                f"未落盘：{len(result['pending'])} 项（footer 已显示重试按钮）",
            )
        return result

    def _retry(self) -> None:
        if not self._last_values:
            self.status_changed.emit("没有可重试的提交")
            return
        self.state.simulate_pending = False
        self.status_changed.emit("重试：已关闭模拟失败，重新提交")
        self.submit(SubmitMode.COLLECT_ERRORS, self._last_values)

    # ------------------------------------------------------------------
    # 假后端的内部逻辑
    # ------------------------------------------------------------------

    def _store(
            self, values: Mapping[str, Any], changed: list[str], *,
            pending: bool,
    ) -> None:
        for key, value in values.items():
            self.state.values[key] = deepcopy(value)
        self.state.dirty = set(changed) if pending else set()
        self.state.pending = set(changed) if pending else set()

    def _current_value(self, key: str) -> Any:
        leaf = _leaf(key)
        if key in self.state.values:
            return deepcopy(self.state.values[key])
        return deepcopy(leaf.get("default"))

    def _changes(self, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for key, value in values.items():
            leaf = _leaf(key)
            old = self._current_value(key)
            changes.append({
                "key": key,
                "path": leaf.get("path"),
                "section": leaf.get("section"),
                "config_key": leaf.get("config_key"),
                "old": old,
                "new": deepcopy(value),
                "text": format_value(value),
                "file": leaf.get("target_file"),
                "changed": not _same(old, value),
            })
        return changes

    def _by_file(self, keys: list[str]) -> dict[str, list[str]]:
        by_file: dict[str, list[str]] = {}
        for key in keys:
            target = _leaf(key).get("target_file")
            if target:
                by_file.setdefault(str(target), []).append(key)
        return by_file

    def _validate(
            self, values: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        accent = values.get("ui.accent_color")
        if isinstance(accent, str) and not _ACCENT_RE.match(accent):
            errors.append({
                "key": "ui.accent_color",
                "path": _leaf("ui.accent_color")["path"],
                "message": "强调色必须是 #RRGGBB 形式（例如 #3d7eff）",
            })
        batch = values.get("thumbnail.batch_size")
        if isinstance(batch, int) and batch % 8 != 0:
            errors.append({
                "key": "thumbnail.batch_size",
                "path": _leaf("thumbnail.batch_size")["path"],
                "message": f"批大小必须是 8 的倍数（当前 {batch}）",
            })
        title = values.get("ui.window_title")
        if isinstance(title, str) and not title.strip():
            errors.append({
                "key": "ui.window_title",
                "path": _leaf("ui.window_title")["path"],
                "message": "窗口标题不能为空",
            })
        return errors


def _leaf(key: str) -> dict[str, Any]:
    try:
        return leaf_index()[key]
    except KeyError as exc:
        raise KeyError(f"demo setting key {key!r} is not declared") from exc


def _same(left: Any, right: Any) -> bool:
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001
        return False


class SettingsDemoWindow(QWidget):
    """演示窗口：顶部是特制 controller 的操作台，下面是纯被动视图。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("albusWall · 设置被动视图演示")
        self.resize(1220, 860)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        demo_bar = QFrame(self)
        demo_bar.setObjectName("DemoBar")
        demo_bar.setStyleSheet(
            "QFrame#DemoBar { background-color: rgba(28,28,28,0.92);"
            " border: 1px solid rgba(90,90,90,0.65); border-radius: 12px; }"
            "QLabel#DemoTitle { font-size: 1.05em; font-weight: bold;"
            " color: rgba(255,255,255,0.95); }"
            "QLabel#DemoHint { color: rgba(190,190,190,0.85); }",
        )
        bar_layout = QVBoxLayout(demo_bar)
        bar_layout.setContentsMargins(12, 8, 12, 8)
        bar_layout.setSpacing(6)

        title = QLabel("特制 presenter / controller（模拟写回层 + 通知源）", demo_bar)
        title.setObjectName("DemoTitle")
        hint = QLabel(
            "视图只做三件事：吃 dump、接通知、吐 {key: typed}。"
            "下面这些按钮全部走 DemoPresenter，不碰视图内部。"
            "试试把「强调色」改成 abc、或把「批大小」改成 65，再点保存看行内错误；"
            "叶子行右键可移动分组，右上角「＋ 组件」可加 about 分组。",
            demo_bar,
        )
        hint.setObjectName("DemoHint")
        hint.setWordWrap(True)
        bar_layout.addWidget(title)
        bar_layout.addWidget(hint)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        reload_button = _demo_button("重载 dump", demo_bar)
        set_button = _demo_button("外部 set：批大小=128", demo_bar)
        remove_button = _demo_button("外部 remove：窗口标题", demo_bar)
        reset_button = _demo_button("外部 reset：主题", demo_bar)
        thread_button = _demo_button("子线程 set：强调色", demo_bar)
        self._pending_toggle = QCheckBox("模拟写盘失败", demo_bar)
        self._pending_toggle.setStyleSheet("color: rgba(230,230,230,0.92);")
        for widget in (
            reload_button, set_button, remove_button, reset_button,
            thread_button, self._pending_toggle,
        ):
            button_row.addWidget(widget)
        button_row.addStretch(1)
        bar_layout.addLayout(button_row)

        self.status = QLabel("演示就绪：设置页的 dump 由 DemoSettingsPresenter 提供", demo_bar)
        self.status.setObjectName("DemoHint")
        self.status.setWordWrap(True)
        bar_layout.addWidget(self.status)
        root.addWidget(demo_bar, 0)

        self.view = SettingView(self)
        self.presenter = DemoSettingsPresenter(self.view, parent=self)
        self.presenter.status_changed.connect(self.status.setText)
        root.addWidget(self.view, 1)

        reload_button.clicked.connect(
            lambda: self.presenter.reload_from_model(),
        )
        set_button.clicked.connect(
            lambda: self._safe("external_set", "thumbnail.batch_size", 128),
        )
        remove_button.clicked.connect(
            lambda: self._safe("external_remove", "ui.window_title"),
        )
        reset_button.clicked.connect(
            lambda: self._safe("external_reset", "ui.theme"),
        )
        thread_button.clicked.connect(
            lambda: self._safe("threaded_set", "ui.accent_color", "#ff5f56"),
        )
        self._pending_toggle.toggled.connect(
            self.presenter.set_simulate_pending,
        )

    def _safe(self, method: str, *args: Any) -> None:
        try:
            getattr(self.presenter, method)(*args)
        except Exception as exc:  # noqa: BLE001  演示窗口不吞错，直接显示
            self.status.setText(f"{method} 失败：{exc}")


def _demo_button(text: str, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.setStyleSheet(
        "QPushButton { min-height: 28px; padding: 0 12px;"
        " border: 1px solid rgba(255,255,255,0.16); border-radius: 14px;"
        " background-color: rgba(96,96,96,0.35); color: rgba(235,235,235,0.92); }"
        "QPushButton:hover { background-color: rgba(150,150,150,0.55); }",
    )
    return button


def _apply_dark_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#242424"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#3a3a3a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f0f0f0"))
    app.setPalette(palette)


def _load_theme_qss() -> str:
    try:
        from albuswall.resources import qss as qss_path

        return Path(qss_path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001  主题缺失不影响演示
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="设置页被动视图演示")
    parser.add_argument(
        "--smoke", action="store_true",
        help="启动后自动退出（CI / offscreen 冒烟）",
    )
    parser.add_argument(
        "--style", choices=("theme", "none"), default="theme",
        help="是否套用应用自带主题 QSS",
    )
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    _apply_dark_palette(app)
    if args.style == "theme":
        app.setStyleSheet(_load_theme_qss())

    window = SettingsDemoWindow()
    window.show()

    if args.smoke:
        # 冒烟：走一遍外部通知 + 子线程通知 + 不弹窗的草稿提交
        def smoke() -> None:
            window.presenter.external_set("thumbnail.batch_size", 128)
            window.presenter.external_reset("ui.theme")
            window.presenter.threaded_set("ui.accent_color", "#ff5f56")
            window.presenter.submit(
                SubmitMode.DRAFT,
                window.view.collect_values(),
            )
            app.quit()

        QTimer.singleShot(120, smoke)
        QTimer.singleShot(2000, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
