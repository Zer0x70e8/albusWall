#
"""UI 文案 / 运行时语言切换的基础设施（i18n 前置条件）。

本包**不**包含任何翻译内容（``.ts`` / ``.qm`` 都还没有实现），只提供让
「整个软件可以动态变更语言」成立所必需的三件事：

* :func:`tr` —— 没有 ``QObject`` 上下文时的翻译入口；
* :class:`TranslationManager` / :func:`translation_manager` —— 装 / 卸
  ``QTranslator``、记录当前语言、广播 ``language_changed``，并在切换后
  主动刷新所有实现了 ``retranslate_ui()`` 的控件；
* :class:`Retranslatable` —— 给 ``QWidget`` 用的混入，把
  ``QEvent.LanguageChange`` 转成一次 ``retranslate_ui()``。

约定（新增界面文案时照做）：

1. 控件里能拿到 ``self`` 就用 ``self.tr("...")``；纯函数 / model / delegate
   用 :func:`tr("Context", "...")`；
2. 文案在 ``_setup_ui`` 里**只建控件不赋值**，统一挪到 ``retranslate_ui()``；
3. presenter 不会收到 Qt 事件，改接
   ``translation_manager().language_changed`` 并实现 ``retranslate()``；
4. 用作文案**标识**的字符串（例如搜索栏模式名）必须与展示文本分离。
"""

from .manager import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Language,
    TranslationManager,
    current_language,
    language_choices,
    language_label,
    translation_manager,
    tr,
)
from .retranslate import Retranslatable

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "Language",
    "Retranslatable",
    "TranslationManager",
    "current_language",
    "language_choices",
    "language_label",
    "translation_manager",
    "tr",
]
