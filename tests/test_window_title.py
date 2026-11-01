#
"""自绘边框窗口的标题同步：改窗口标题，标题栏标题自动跟着变。

约定（勿混淆）：

* **标题栏**（``TitleBar.action_bar``）左侧 ``window_title`` 是本用例的
  被测对象 —— 它镜像 ``QWidget.windowTitle()``；
* **工具栏**（``TitleBar.title_label`` / ``sub_title``）是业务文案，
  与窗口标题毫无关系，这里的用例顺带断言它们不被误改。

Qt 需要 offscreen 平台才能在无显示环境跑；必须在 import PySide6 之前设好。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(qapp):
    """真实主窗口；标题相关断言只在标题栏上做。"""
    from albuswall.ui.window import Window

    w = Window()
    try:
        yield w
    finally:
        w.close()


# ==================== 窗口 → 标题栏 ====================

def test_set_window_title_reaches_title_bar(window):
    window.setWindowTitle("albusWall · 相册")
    assert window.windowTitle() == "albusWall · 相册"
    assert window.title_bar.window_title.text() == "albusWall · 相册"
    assert window.title_bar.window_title.isVisibleTo(window)


def test_snake_case_alias(window):
    window.set_window_title("Snake Case")
    assert window.windowTitle() == "Snake Case"
    assert window.title_bar.window_title.text() == "Snake Case"


def test_title_updates_are_repeatable(window):
    for title in ("A", "B", "C"):
        window.setWindowTitle(title)
        assert window.title_bar.window_title.text() == title


def test_empty_title_hides_label(window):
    window.setWindowTitle("有标题")
    assert window.title_bar.window_title.isVisibleTo(window) is True

    window.setWindowTitle("")
    assert window.title_bar.window_title.text() == ""
    assert window.title_bar.window_title.isVisibleTo(window) is False


def test_change_event_resyncs_after_desync(window):
    """兜底路径：changeEvent 收到 WindowTitleChange 就把两边拉回一致。"""
    window.setWindowTitle("窗口标题")
    window.title_bar.set_window_title("被人改坏了")  # 人为制造不一致

    window.changeEvent(QEvent(QEvent.Type.WindowTitleChange))

    assert window.title_bar.window_title.text() == "窗口标题"


# ==================== 别碰工具栏 ====================

def test_tool_bar_title_is_independent(window):
    window.title_bar.set_title("相册主标题")
    window.title_bar.set_description("相册副标题")

    window.setWindowTitle("窗口标题")

    # 工具栏文案原封不动
    assert window.title_bar.title_label.text() == "相册主标题"
    assert window.title_bar.sub_title.text() == "相册副标题"
    # 标题栏镜像窗口标题
    assert window.title_bar.window_title.text() == "窗口标题"


def test_title_bar_none_is_ignored(window):
    """``None`` 沿用旧行为：忽略，不清空已显示的文字。"""
    window.setWindowTitle("保留我")
    window.title_bar.set_window_title(None)
    assert window.title_bar.window_title.text() == "保留我"
