#
"""运行时语言切换的基础设施（**不**包含任何翻译内容）。

这一层的职责只有三件事：

1. 维护「当前语言」这份状态，并把语言代码和 ``QTranslator`` 关联起来；
2. 装 / 卸翻译器之后**主动**刷新界面文案 —— Qt 只会把
   ``QEvent.LanguageChange`` 发给顶层窗口，嵌套控件收不到，所以这里遍历
   ``QApplication.allWidgets()`` 逐个调用 ``retranslate_ui()``；
3. 广播 :attr:`TranslationManager.language_changed`，让非控件对象
   （presenter / model / delegate）自己重算文案。

翻译文件由 ``scripts/build_i18n.py`` 生成（词表在
``scripts/i18n_catalog.py``），运行时从 ``resources/i18n`` 读取：

* ``albuswall/resources/i18n/albuswall_<lang>.qm``  —— 本项目的文案；
* PySide6 自带的 ``qtbase_<lang>.qm`` —— Qt 标准对话框按钮等。

找不到翻译文件时**不报错**：界面文案回落到源码里的原文，语言状态照常记录
并广播，因此缺一个语种的发行包仍然能正常启动。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QObject,
    QTranslator,
    Signal,
)
from PySide6.QtWidgets import QApplication

from ...languages import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Language,
    language_choices,
    language_label,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "Language",
    "TranslationManager",
    "current_language",
    "language_choices",
    "language_label",
    "translation_manager",
    "tr",
]

_logger = logging.getLogger(__name__)

#: 语言代码 → 声明（``normalize`` 的收敛依据）
_LANGUAGE_BY_CODE = {language.code: language for language in LANGUAGES}


def tr(context: str, text: str) -> str:
    """没有 ``QObject`` 上下文时的翻译入口。

    ``self.tr(...)`` 只对 ``QObject`` 子类可用；model / delegate / 纯函数里
    需要翻译时用本函数，并显式给出上下文（建议用所在类名）。
    """
    return QCoreApplication.translate(str(context), text)


# ══════════════════════════════════════════════════
# 翻译器管理
# ══════════════════════════════════════════════════
def _application_translation_dir() -> Path:
    """本项目的翻译文件目录：``resources/i18n``（别名 ``i18n``）。

    目录不存在时（例如发行包里漏了一整个资源包）只记 debug 日志并回落到
    ``resources/CURRENT_PATH``，让 ``_load_translator`` 静默跳过即可。
    """
    try:
        from albuswall.resources import get

        return Path(get("i18n", as_folder=True))
    except Exception:  # noqa: BLE001  资源包异常也不该让语言切换失效
        _logger.debug("cannot resolve i18n resource dir", exc_info=True)
        try:
            from albuswall.resources import CURRENT_PATH

            return Path(CURRENT_PATH) / "i18n"
        except Exception:  # noqa: BLE001
            return Path(__file__).resolve().parents[3] / "resources" / "i18n"


def _qt_translation_dir() -> Optional[Path]:
    """PySide6 / Qt 自带翻译文件目录。"""
    try:
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    except Exception:  # noqa: BLE001
        return None
    return Path(path) if path else None


def _load_translator(
        directory: Optional[Path],
        filename_stem: str,
) -> Optional[QTranslator]:
    """在 ``directory`` 里找 ``<stem>.qm``；找不到返回 ``None``（不报错）。"""
    if directory is None:
        return None
    candidate = Path(directory) / f"{filename_stem}.qm"
    if not candidate.is_file():
        _logger.debug("translation file not found: %s", candidate)
        return None
    translator = QTranslator()
    if not translator.load(str(candidate)):
        _logger.warning("failed to load translation file: %s", candidate)
        return None
    _logger.debug("loaded translation file: %s", candidate)
    return translator


class TranslationManager(QObject):
    """装载 / 卸载翻译器，并驱动整个界面重新取文案。

    用法（正常只经 :func:`translation_manager` 拿单例）::

        manager = translation_manager()
        manager.language_changed.connect(some_presenter.retranslate)
        manager.set_language("zh_CN")      # 立即生效 + 落广播
        manager.install()                  # 从配置读一次（启动时用）

    实现要点
    --------
    * **始终保留源码原文作为兜底**：没有翻译文件时界面显示原文，切换语言
      仍然会广播 ``language_changed``；presenter 可以在自己的
      ``retranslate()`` 里做「语言相关但非翻译」的刷新（例如重新载入模型）。
    * **控件刷新是主动遍历**：仅靠 Qt 的 ``LanguageChange`` 事件只能覆盖
      顶层窗口；嵌套控件必须由本类逐个调用 ``retranslate_ui()``。
    * **重复调用幂等**：同一语言重复 ``install`` 只会重新装一次翻译器。
    """

    #: 语言切换完成：载荷是新语言代码（``"en_US"`` / ``"zh_CN"`` / ...）。
    language_changed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._language: str = DEFAULT_LANGUAGE
        self._translators: list[QTranslator] = []
        self._installed: bool = False

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def language(self) -> str:
        """当前语言代码。"""
        return self._language

    @property
    def installed(self) -> bool:
        """是否已经装过翻译器（``install`` 成功过）。"""
        return self._installed

    def normalize(self, language: object) -> str:
        """把任意输入收敛成一个语言代码；空值 / 未知值回落默认语言。"""
        code = str(language or "").strip()
        if not code:
            return DEFAULT_LANGUAGE
        if code in _LANGUAGE_BY_CODE:
            return code
        # 允许只写主语言（``zh`` / ``en``）或带修饰（``zh_CN.UTF-8``）
        lowered = code.lower().replace("-", "_")
        for language_ in LANGUAGES:
            if lowered == language_.code.lower():
                return language_.code
        for language_ in LANGUAGES:
            if lowered.split("_")[0] == language_.code.split("_")[0].lower():
                return language_.code
        _logger.debug("unknown language %r, keep as-is", code)
        return code

    def language_from_config(self) -> str:
        """从配置读语言偏好（``[ui] language``，与主题同属 UI 引导配置）。

        读不到时返回 :data:`DEFAULT_LANGUAGE`；配置系统未就绪也不抛异常。
        """
        try:
            # i18n 在 ``ui.builtin`` 下，``ui.bootstrap`` 要往上三层
            from ...bootstrap import ui_config

            value = ui_config().get("language")
        except Exception:  # noqa: BLE001  配置未就绪时保持默认
            _logger.debug("language config unavailable", exc_info=True)
            return DEFAULT_LANGUAGE
        return self.normalize(value)

    # ------------------------------------------------------------------ #
    # 装载 / 切换
    # ------------------------------------------------------------------ #
    def install(self, language: Optional[str] = None) -> bool:
        """装载（或切换）语言。返回是否成功装到 QApplication。

        ``language=None`` 表示「从配置读」；显式传值时只切换，不写配置 ——
        持久化是调用方（设置页）的职责。
        """
        app = QApplication.instance()
        if app is None:
            # 没有 GUI 也能改状态：headless 场景下只记录 + 广播
            target = self.normalize(
                language if language is not None else self.language_from_config()
            )
            changed = target != self._language
            self._language = target
            if changed:
                self.language_changed.emit(target)
            return False

        target = self.normalize(
            language if language is not None else self.language_from_config()
        )
        if self._installed and target == self._language:
            # 同一语言重复「切换」= no-op：避免白白重建设置页之类的重活
            return True

        self._remove_translators(app)
        self._add_translators(app, target)
        self._installed = True
        self._language = target

        # 先刷新控件再广播：presenter 的 retranslate 里可能会改模型，
        # 让「控件 → 模型 → 视图」的刷新顺序对用户可见结果保持一致。
        self.retranslate_widgets()
        self.language_changed.emit(target)
        return True

    def set_language(self, language: str) -> bool:
        """切换到一个明确的语言（``install`` 的语义化别名）。"""
        return self.install(language)

    # ------------------------------------------------------------------ #
    # 翻译器装卸
    # ------------------------------------------------------------------ #
    def _remove_translators(self, app: QApplication) -> None:
        # 后装的先卸：与 Qt 的「后装优先」规则对称
        for translator in reversed(self._translators):
            try:
                app.removeTranslator(translator)
            except RuntimeError:  # pragma: no cover - C++ 对象已销毁
                pass
        self._translators.clear()

    def _add_translators(self, app: QApplication, language: str) -> None:
        # qtbase：Qt 标准对话框 / 内置控件文案。先装，优先级低于本项目的翻译。
        qt_translator = _load_translator(
            _qt_translation_dir(), f"qtbase_{language}"
        )
        if qt_translator is not None:
            app.installTranslator(qt_translator)
            self._translators.append(qt_translator)

        app_translator = _load_translator(
            _application_translation_dir(), f"albuswall_{language}"
        )
        if app_translator is not None:
            app.installTranslator(app_translator)
            self._translators.append(app_translator)

    # ------------------------------------------------------------------ #
    # 控件刷新
    # ------------------------------------------------------------------ #
    def retranslate_widgets(self) -> None:
        """遍历全部控件，调用其中实现了 ``retranslate_ui()`` 的那些。

        这是 Qt 事件机制的必要补充：``QEvent.LanguageChange`` 只发给顶层
        窗口，而本项目把文案分散在各个嵌套控件里。
        """
        app = QApplication.instance()
        if app is None:
            return
        try:
            widgets = list(app.allWidgets())
        except RuntimeError:  # pragma: no cover - 应用正在销毁
            return

        for widget in widgets:
            callback = getattr(widget, "retranslate_ui", None)
            if not callable(callback):
                continue
            try:
                callback()
            except RuntimeError:
                # 控件已进入销毁队列：跳过即可
                continue
            except Exception:  # noqa: BLE001  单个控件失败不拖垮整次切换
                _logger.exception(
                    "retranslate_ui failed for %r", type(widget).__name__
                )


# ══════════════════════════════════════════════════
# 单例
# ══════════════════════════════════════════════════
_MANAGER: Optional[TranslationManager] = None


def translation_manager() -> TranslationManager:
    """进程级单例。

    首次调用时才创建：``QApplication`` 可能还没构造，而翻译器装载必须
    等到它存在之后（见 :meth:`TranslationManager.install`）。
    """
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TranslationManager()
    return _MANAGER


def current_language() -> str:
    """当前语言代码（便捷入口）。"""
    return translation_manager().language
