#
"""动态语言切换的前置条件：``Retranslatable`` / ``TranslationManager``。

覆盖：

  1. 语言注册表（``LANGUAGES`` / ``language_choices`` / ``language_label``）
     和 ``normalize`` 的收敛规则；
  2. ``install()`` 在没有 ``.qm`` 翻译文件时也能成功切语言（界面回落到源码
     原文），并且会把状态记下来；
  3. 所有实现了 ``retranslate_ui()`` 的控件在切语言后都会被重新调用一遍
     —— 包括**嵌套**控件（Qt 自己的 ``LanguageChange`` 只发给顶层窗口）；
  4. 真实的界面控件（标题栏 / 菜单 / 详情页 / 标签栏 / 搜索栏 / 源详情页 /
     标签编辑对话框）都有 ``retranslate_ui``，且文案非空、重复调用幂等；
  5. 搜索栏的「模式」是稳定标识而不是展示文本（语言切换不能影响判断）；
  6. ``resources/i18n`` 里的 ``.ts`` / ``.qm`` 存在且真的能翻译
     （中英两个方向都验证一遍）；
  7. 默认界面语言是中文（``DEFAULT_LANGUAGE``）。

不依赖 pytest：``python test/test_i18n_retranslate.py`` 直接运行；
用 offscreen Qt 平台，不需要显示器。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 强制离屏：桌面会话里可能已有 QT_QPA_PLATFORM=wayland;xcb，
# setdefault 会被它绕过 —— 测试就会真的弹窗口。
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from albuswall.resources import get as resource_path  # noqa: E402

from albuswall.ui.builtin.i18n import (  # noqa: E402
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Retranslatable,
    current_language,
    language_choices,
    language_label,
    translation_manager,
    tr,
)

_app = QApplication.instance() or QApplication([])

_TARGET = LANGUAGES[1].code if len(LANGUAGES) > 1 else DEFAULT_LANGUAGE


# ══════════════════════════════════════════════════════════════
# 语言注册表
# ══════════════════════════════════════════════════════════════
def test_language_registry() -> None:
    codes = language_choices()
    assert DEFAULT_LANGUAGE in codes
    assert len(codes) == len(set(codes))
    for language in LANGUAGES:
        assert language_label(language.code) == language.native_name
    # 未知代码原样返回：配置写错时要看得见，而不是静默变默认
    assert language_label("xx_YY") == "xx_YY"
    assert language_label(None) == ""


def test_normalize() -> None:
    manager = translation_manager()
    assert manager.normalize(None) == DEFAULT_LANGUAGE
    assert manager.normalize("") == DEFAULT_LANGUAGE
    assert manager.normalize(_TARGET) == _TARGET
    # 大小写 / 连字符 / 只写主语言都能收敛
    assert manager.normalize(_TARGET.lower()) == _TARGET
    assert manager.normalize(_TARGET.replace("_", "-")) == _TARGET
    assert manager.normalize(_TARGET.split("_")[0]) == _TARGET
    assert manager.normalize("xx_YY") == "xx_YY"


def test_tr_helper_returns_source_without_translation() -> None:
    """没有 .qm 时 ``tr`` 原样返回；这保证切换语言不会把界面变空。"""
    assert tr("TestContext", "hello") == "hello"


# ══════════════════════════════════════════════════════════════
# 资源包里的翻译文件
# ══════════════════════════════════════════════════════════════
def test_i18n_resource_files_exist() -> None:
    """每种语言都要有 .ts（可编辑）和 .qm（运行期可加载）。"""
    directory = resource_path("i18n", as_folder=True)
    assert directory.is_dir()
    for language in LANGUAGES:
        for suffix in (".ts", ".qm"):
            path = directory / f"albuswall_{language.code}{suffix}"
            assert path.is_file(), f"缺少 {path.name}"
            assert path.stat().st_size > 0, f"{path.name} 是空文件"


def test_catalogs_translate_known_strings() -> None:
    """.qm 实际生效：同一个上下文在不同语言下得到不同文案。"""
    manager = translation_manager()
    try:
        manager.install("zh_CN")
        assert QCoreApplication.translate("Menu", "source") == "来源"
        assert QCoreApplication.translate("Setting", "Settings") == "设置"
        assert (
            QCoreApplication.translate("SettingSchema", "Appearance")
            == "外观"
        )
        # 源文为中文的条目在 zh_CN 下保持原文
        assert (
            QCoreApplication.translate("TagBar", "标签名，可用 / 分层")
            == "标签名，可用 / 分层"
        )

        manager.install("en_US")
        assert QCoreApplication.translate("Menu", "source") == "source"
        assert QCoreApplication.translate("MetadataModel", "标题") == "Title"
        assert (
            QCoreApplication.translate("InfoCellDelegate", "剩余 {days} 天")
            == "{days} days left"
        )
        assert (
            QCoreApplication.translate("SettingSchema", "Appearance")
            == "Appearance"
        )
    finally:
        # 恢复默认语言，避免影响同进程里的其它用例
        manager.install(DEFAULT_LANGUAGE)


def test_default_language_is_chinese() -> None:
    """默认界面语言是中文；英文仍可从设置页切回去。"""
    manager = translation_manager()
    try:
        manager.install(DEFAULT_LANGUAGE)
        assert current_language() == "zh_CN"
        assert QCoreApplication.translate("Menu", "setting") == "设置"
        # 中文源文在 zh_CN 下不需要条目
        assert (
            QCoreApplication.translate("TagBar", "标签名，可用 / 分层")
            == "标签名，可用 / 分层"
        )

        manager.install("en_US")
        assert QCoreApplication.translate("Menu", "setting") == "setting"
        assert QCoreApplication.translate("MetadataModel", "标题") == "Title"
    finally:
        manager.install(DEFAULT_LANGUAGE)


# ══════════════════════════════════════════════════════════════
# 刷新机制
# ══════════════════════════════════════════════════════════════
class _Probe(Retranslatable, QWidget):
    """记录 ``retranslate_ui`` 调用次数的最小控件。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.calls = 0
        self.label = QLabel("", self)
        self.retranslate_ui()  # 首次文案：与真实控件的约定一致

    def retranslate_ui(self) -> None:
        self.calls += 1
        self.label.setText(self.tr("probe"))


def test_nested_widgets_are_retranslated() -> None:
    manager = translation_manager()
    window = QWidget()
    probe = _Probe(window)  # 嵌套控件：Qt 自己不会把 LanguageChange 发给它
    probe.show()
    window.show()

    first = probe.calls
    assert first >= 1

    manager.set_language(_TARGET)
    assert probe.calls > first, "嵌套控件没有被重新翻译"

    # 同一语言重复切换是 no-op（不重建界面）；控件自己的 retranslate_ui
    # 仍必须幂等可调用（presenter 主动刷新时代码路径相同）。
    before = probe.calls
    manager.set_language(_TARGET)
    assert probe.calls == before
    probe.retranslate_ui()
    assert probe.calls == before + 1
    assert probe.label.text() != ""
    assert current_language() == _TARGET

    window.close()


def test_install_records_language_without_qm_files() -> None:
    manager = translation_manager()
    ok = manager.install(_TARGET)
    assert ok is True
    assert manager.installed is True
    assert manager.language == _TARGET


# ══════════════════════════════════════════════════════════════
# 真实控件的 retranslate_ui
# ══════════════════════════════════════════════════════════════
def _make_widgets() -> list:
    """造一组真实控件（尽量不依赖配置 / 数据库）。"""
    from albuswall.ui.builtin.dialog.tag_edit_dialog import TagEditDialog
    from albuswall.ui.builtin.window.menu import Menu
    from albuswall.ui.builtin.window.source.detail_widget import (
        IngestSourceDetailWidget,
    )
    from albuswall.ui.builtin.window.tagbar import TagBar
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
        SearchBarLayoutPlaceholder,
    )

    host = QWidget()
    placeholder = SearchBarLayoutPlaceholder(host)
    search_bar = FloatingSearchBar(host, host, placeholder)

    widgets = [
        search_bar,
        search_bar._suggestions,
        Menu(host),
        TagBar(host),
        IngestSourceDetailWidget(host),
        TagEditDialog("create", parent=host),
    ]
    return widgets


def test_real_widgets_retranslate() -> None:
    manager = translation_manager()
    widgets = _make_widgets()

    for widget in widgets:
        callback = getattr(widget, "retranslate_ui", None)
        assert callable(callback), f"{type(widget).__name__} 没有 retranslate_ui"

    # 切语言两次（去 / 回），每次都必须能在不抛异常的前提下重放文案
    for language in (_TARGET, DEFAULT_LANGUAGE):
        manager.set_language(language)
        for widget in widgets:
            widget.retranslate_ui()


def test_real_widget_texts_are_not_empty() -> None:
    from albuswall.ui.builtin.window.menu import Menu
    from albuswall.ui.builtin.window.source.detail_widget import (
        IngestSourceDetailWidget,
    )
    from albuswall.ui.builtin.window.tagbar import TagBar
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
        SearchBarLayoutPlaceholder,
    )

    host = QWidget()
    placeholder = SearchBarLayoutPlaceholder(host)
    search_bar = FloatingSearchBar(host, host, placeholder)
    search_bar.retranslate_ui()

    texts = [
        search_bar.line_edit.placeholderText(),
        search_bar.option_combo.itemText(0),
        search_bar.option_combo.itemText(1),
    ]
    menu = Menu(host)
    menu.retranslate_ui()
    texts += [action.text() for action in menu.actions() if action.text()]
    tag_bar = TagBar(host)
    tag_bar.retranslate_ui()
    texts += [
        tag_bar.tag_line_edit.placeholderText(),
        tag_bar.tag_apply_button.text(),
        tag_bar.layer_tree_button.text(),
        tag_bar.tag_create_button.text(),
    ]
    detail = IngestSourceDetailWidget(host)
    detail.retranslate_ui()
    texts += [
        detail.title_line_edit.placeholderText(),
        detail.grp_meta.title(),
        detail.grp_source.title(),
        detail.grp_triggers.title(),
        detail.update_mode_combo.itemText(0),
        detail.update_mode_combo.itemText(1),
    ]
    assert all(texts), texts


def test_search_bar_mode_is_stable_identifier() -> None:
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
        SearchBarLayoutPlaceholder,
    )

    host = QWidget()
    placeholder = SearchBarLayoutPlaceholder(host)
    search_bar = FloatingSearchBar(host, host, placeholder)

    assert search_bar.mode == FloatingSearchBar.MODE_SIMPLE
    # 标识不能是展示文本：语言切换后模式判断仍成立
    search_bar.set_mode(FloatingSearchBar.MODE_COMMAND)
    assert search_bar.mode == FloatingSearchBar.MODE_COMMAND
    translation_manager().set_language(_TARGET)
    search_bar.retranslate_ui()
    assert search_bar.mode == FloatingSearchBar.MODE_COMMAND
    search_bar.set_mode(FloatingSearchBar.MODE_SIMPLE)
    assert search_bar.mode == FloatingSearchBar.MODE_SIMPLE


# ══════════════════════════════════════════════════════════════
# 手跑入口
# ══════════════════════════════════════════════════════════════
def _main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures: list[tuple[str, str]] = []
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001
            failures.append((test.__name__, traceback.format_exc()))
            print(f"[FAIL] {test.__name__}")
        else:
            print(f"[ OK ] {test.__name__}")
    print()
    for name, detail in failures:
        print(f"===== {name} =====\n{detail}")
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
