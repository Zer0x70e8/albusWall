#
"""语言设置接口：字面量规范化 + 运行期切换界面语言。

Qt 需要 offscreen 平台才能在无显示环境跑；必须在 import PySide6 之前设好。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from albuswall.ui.i18n import (  # noqa: E402
    TranslationManager,
    available_languages,
    current_language,
    normalize_language,
    set_language,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def manager(qapp):
    """每个用例一个干净的管理器，结束后撤掉翻译器。"""
    m = TranslationManager(qapp)
    yield m
    m.reset()


# ==================== 字面量规范化 ====================

@pytest.mark.parametrize("text, expected", [
    ("zh_CN", "zh_CN"),
    ("en_US", "en_US"),
    ("zh-CN", "zh_CN"),
    ("en-us", "en_US"),
    ("  ZH_cn  ", "zh_CN"),
    ("zh", "zh_CN"),
    ("en", "en_US"),
    ("简体中文", "zh_CN"),
    ("English", "en_US"),
])
def test_normalize_language_accepts_literals(text, expected):
    assert normalize_language(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "fr_FR", "日本語", "zz"])
def test_normalize_language_rejects_unknown(text):
    assert normalize_language(text) is None


def test_available_languages_lists_codes(qapp):
    assert available_languages() == ("zh_CN", "en_US")


# ==================== 管理器 ====================

def test_unknown_language_keeps_current(manager):
    assert manager.set_language("en_US") is True
    assert manager.set_language("fr_FR") is False
    assert manager.language == "en_US"
    assert manager.translator is not None


def test_same_language_is_idempotent(manager):
    assert manager.set_language("zh_CN") is True
    assert manager.set_language("zh_CN") is True
    assert manager.language == "zh_CN"


def test_reset_returns_to_source(manager):
    assert manager.set_language("en_US") is True
    manager.reset()
    assert manager.language == ""
    assert manager.translator is None


def test_translator_translates(manager):
    """源文是英文：en_US 直接回退源文，zh_CN 才带英文 → 中文的条目。"""
    from PySide6.QtCore import QCoreApplication

    manager.set_language("en_US")
    en = manager.translator
    assert en is not None
    assert en.translate("TitleBar", "Album") == ""
    assert QCoreApplication.translate("TitleBar", "Album") == "Album"
    assert QCoreApplication.translate("Menu", "Quit") == "Quit"

    manager.set_language("zh_CN")
    zh = manager.translator
    assert zh is not None
    assert zh.translate("TitleBar", "Album") == "相册"
    assert QCoreApplication.translate("Menu", "Quit") == "退出"


# ==================== 运行期切换（真实窗口） ====================

def test_switching_updates_live_widgets(qapp, manager):
    from albuswall.ui.window import Window

    window = Window()
    window.resize(640, 480)
    window.show()
    qapp.processEvents()
    try:
        # 默认（没装翻译器）显示英文源文
        assert window.title_bar.album_button.text() == "Album"
        assert window.menu.source_action.text() == "Import Source"
        assert window.detail.trash_button.text() == "Move to Trash"

        assert manager.set_language("en_US") is True
        qapp.processEvents()
        assert window.title_bar.album_button.text() == "Album"
        assert window.menu.source_action.text() == "Import Source"
        assert window.detail.trash_button.text() == "Move to Trash"

        assert manager.set_language("zh_CN") is True
        qapp.processEvents()
        assert window.title_bar.album_button.text() == "相册"
        assert window.menu.source_action.text() == "导入源"
        assert window.detail.trash_button.text() == "移入回收站"
    finally:
        window.close()


def test_module_level_set_language(qapp):
    try:
        assert set_language("en_US") is True
        assert current_language() == "en_US"
        assert set_language("zh_CN") is True
        assert current_language() == "zh_CN"
    finally:
        from albuswall.ui.i18n import get_manager

        get_manager().reset()
