#
"""``albuswall.ui`` —— PySide6 界面层（主循环、主窗口、文案基础设施）。

界面语言两个入口（等价）：

* :func:`set_language` —— 直接喂字面量，``set_language("en_US")``；
* :class:`~albuswall.ui.i18n.TranslationManager` —— 应用层持有 / 注入，
  容器里的 ``i18n`` 就是它。
"""

from __future__ import annotations

from .application import Application, UIApplication
from .i18n import (
    TranslationManager,
    available_languages,
    current_language,
    set_language,
)

__all__ = [
    "Application",
    "TranslationManager",
    "UIApplication",
    "available_languages",
    "current_language",
    "set_language",
]
