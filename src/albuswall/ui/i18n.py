#
"""UI 文案基础设施：``Retranslatable`` + 翻译器管理 + 语言设置接口。

三块职责：

1. :class:`Retranslatable` —— 控件收到 ``QEvent.LanguageChange`` 就重跑
   ``retranslate_ui()``。控件在 ``_setup_ui`` 里**只建控件不写文案**，文案
   集中在 ``retranslate_ui()``；该函数必须幂等，只改文本、不重建控件。
2. :class:`TranslationManager` —— 装载 / 卸载 ``QTranslator``；切换语言时
   Qt 会广播 ``LanguageChange``（向下传播到每个子控件），各控件据此刷新。
3. 模块级便捷函数 —— 直接喂字面量就能切语言：

.. code-block:: python

    from albuswall.ui import set_language

    set_language("en_US")      # 也接受 "en" / "en-US" / "English"
    set_language("zh_CN")      # 也接受 "zh" / "zh-CN" / "简体中文"

没有翻译条目的语种（例如 ``en_US``：``self.tr`` 的源文本身就是英文，
``.qm`` 里几乎没有条目）切换时需要手动补一次广播 —— Qt 只在翻译器非空时
才发 ``LanguageChange``。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QTranslator
from PySide6.QtWidgets import QApplication

from albuswall import resources

from .languages import DEFAULT_LANGUAGE, LANGUAGES

__all__ = [
    "DEFAULT_LANGUAGE",
    "Retranslatable",
    "TranslationManager",
    "available_languages",
    "current_language",
    "get_manager",
    "normalize_language",
    "set_language",
]

logger = logging.getLogger(__name__)

#: 全部可选语言代码（``zh_CN`` / ``en_US`` …），顺序同设置页下拉框。
_LANGUAGE_CODES: tuple[str, ...] = tuple(
    language.code for language in LANGUAGES)

#: 语言代码 → 设置页展示用的母语名。
_NATIVE_NAMES: dict[str, str] = {
    language.code: language.native_name for language in LANGUAGES
}


class Retranslatable:
    """语言切换混入，必须放在 Qt 基类**之前**。

    .. code-block:: python

        class Foo(Retranslatable, QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._setup_ui()        # 只建控件
                self.retranslate_ui()   # 首次应用文案

            def retranslate_ui(self) -> None:
                self.title.setText(self.tr("Hello"))
    """

    def retranslate_ui(self) -> None:
        """重新应用本控件的用户可见文案；默认无操作。"""

    # noinspection PyPep8Naming
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)


# --------------------------------------------------------------------------- #
# 语言代码规范化
# --------------------------------------------------------------------------- #
def normalize_language(language: object) -> str | None:
    """把外部给的字面量规范成 ``zh_CN`` / ``en_US`` 这样的语言代码。

    接受这些写法（大小写 / 短横线 / 母语名都无所谓）：

    * 规范代码：``"zh_CN"``、``"en_US"``
    * 短横线 / 大小写变体：``"zh-CN"``、``"EN_us"``
    * 语言前缀（唯一时）：``"zh"``、``"en"``
    * 母语名：``"简体中文"``、``"English"``

    未知写法返回 ``None``。
    """
    text = str(language or "").strip()
    if not text:
        return None

    # 1) 精确命中
    if text in _LANGUAGE_CODES:
        return text

    # 2) 短横线 / 大小写变体
    folded = text.replace("-", "_").casefold()
    for code in _LANGUAGE_CODES:
        if code.casefold() == folded:
            return code

    # 3) 母语名（设置页显示的就是它）
    for code, native_name in _NATIVE_NAMES.items():
        if native_name.casefold() == text.casefold():
            return code

    # 4) 语言前缀（存在多个同前缀语言时不猜）
    matches = [code for code in _LANGUAGE_CODES
               if code.split("_")[0].casefold() == folded]
    return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------- #
# 翻译器管理
# --------------------------------------------------------------------------- #
def _translator_for(code: str) -> QTranslator:
    """按语言代码装载 ``resources/i18n/albuswall_<code>.qm``。

    找不到译文（或资源缺失）时返回**空翻译器**：``tr()`` 回退到源文，
    界面照常可用。
    """
    translator = QTranslator()
    try:
        qm_path: Path = resources.i18n_dir / f"albuswall_{code}.qm"
    except Exception:  # noqa: BLE001 - 资源缺失不该让界面起不来
        logger.debug("i18n 资源目录不可用，%s 回退源文", code, exc_info=True)
        return translator

    if qm_path.is_file() and translator.load(str(qm_path)):
        return translator
    logger.debug("没有可用的翻译文件 %s，界面回退源文", qm_path)
    return translator


class TranslationManager:
    """界面语言的装载 / 切换（由 UI 应用层持有一个实例）。

    切换语言的唯一入口是 :meth:`set_language`，参数就是语言代码字面量::

        manager.set_language("en_US")
    """

    def __init__(self, app: QCoreApplication | None = None) -> None:
        self._app = app if app is not None else QCoreApplication.instance()
        self._translator: QTranslator | None = None
        self._language: str = ""

        # 谁最后建出来谁就是模块级「那一个」：这样 Application 里的管理器和
        # 模块级 set_language() 操作同一份状态，不会各装一个翻译器互相盖。
        global _manager
        _manager = self

    # ---- 查询 ----
    @property
    def language(self) -> str:
        """当前语言代码；还没设置过时为空串（界面显示源文）。"""
        return self._language

    @property
    def translator(self) -> QTranslator | None:
        """当前装载的翻译器（没设置过时为 ``None``）。"""
        return self._translator

    @property
    def available_languages(self) -> tuple[str, ...]:
        """可选语言代码（顺序即设置页顺序）。"""
        return _LANGUAGE_CODES

    # ---- 切换 ----
    def set_language(self, language: str) -> bool:
        """切换到 ``language`` 指定的界面语言；成功返回 ``True``。

        * 未知写法：保持当前语言，返回 ``False``；
        * 与当前语言相同：直接返回 ``True``；
        * 目标语言的 ``.qm`` 缺失或为空：界面回退到源文（``self.tr`` 用英文，
          动态数据标签用中文）。

        切换后 Qt 会广播 ``LanguageChange``（空翻译器时由本类补发），
        控件在事件循环下一轮完成重绘 —— 不要在调用后立刻断言界面文字。
        """
        app = self._app if self._app is not None else QCoreApplication.instance()
        if app is None:
            logger.debug("set_language(%r)：还没有 QCoreApplication，忽略", language)
            return False

        code = normalize_language(language)
        if code is None:
            logger.warning(
                "未知界面语言 %r（可用：%s）", language, ", ".join(_LANGUAGE_CODES))
            return False
        if code == self._language:
            return True

        translator = _translator_for(code)

        # 先撤旧的：旧翻译器非空时 Qt 会在这一步广播 LanguageChange
        old = self._translator
        if old is not None:
            app.removeTranslator(old)
            self._translator = None

        if not app.installTranslator(translator):
            logger.warning("安装翻译器失败：%s", code)
            if old is not None:
                app.installTranslator(old)
                self._translator = old
            return False

        # 空翻译器（源文兜底语种 / 缺 .qm）不会触发 Qt 广播，手动补一次
        if translator.isEmpty():
            self._broadcast(app)

        self._translator = translator
        self._language = code
        logger.debug("界面语言切换为 %s", code)
        return True

    def reset(self) -> None:
        """撤掉自己装的翻译器，界面回到源文（英文 + 少量中文数据）。"""
        app = self._app if self._app is not None else QCoreApplication.instance()
        if app is not None and self._translator is not None:
            app.removeTranslator(self._translator)
        self._translator = None
        self._language = ""

    # ---- 内部 ----
    @staticmethod
    def _broadcast(app: QCoreApplication) -> None:
        """手动广播 LanguageChange（只发给顶层窗口，Qt 会往下传给子控件）。"""
        if not isinstance(app, QApplication):
            return
        event = QEvent(QEvent.Type.LanguageChange)
        for widget in app.topLevelWidgets():
            QCoreApplication.sendEvent(widget, event)


# --------------------------------------------------------------------------- #
# 模块级便捷函数（桌面端只有一个语言状态，懒建单例）
# --------------------------------------------------------------------------- #
_manager: TranslationManager | None = None


def get_manager() -> TranslationManager:
    """模块级共享的 :class:`TranslationManager`（懒创建）。"""
    global _manager
    if _manager is None:
        _manager = TranslationManager()
    return _manager


def set_language(language: str) -> bool:
    """设置界面语言；参数就是语言代码字面量。

    .. code-block:: python

        from albuswall.ui import set_language

        set_language("en_US")   # 或 "en" / "en-US" / "English"
        set_language("zh_CN")   # 或 "zh" / "zh-CN" / "简体中文"

    返回 ``True`` 表示已切换；未知写法返回 ``False``（当前语言不变）。
    """
    return get_manager().set_language(language)


def current_language() -> str:
    """当前界面语言代码；还没设置过时为空串。"""
    return get_manager().language


def available_languages() -> tuple[str, ...]:
    """全部可选语言代码（``("zh_CN", "en_US")``）。"""
    return _LANGUAGE_CODES
