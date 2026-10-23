#
"""界面语言的**纯数据**注册表（不依赖 Qt / 不依赖 GUI 实现）。

放在 ``albuswall.ui`` 根下而不是 UI 实现里，是因为它必须能被「不保证
PySide6 已安装」的代码使用 —— 例如 ``services/command.py`` 登记设置树时
需要语言候选值。真正的翻译器管理（``QTranslator``）在
``albuswall.ui.builtin.i18n`` 里，只由 GUI 侧导入。

新增语言只需要在这里加一行：

.. code-block:: python

    Language("ja_JP", "日本語")

设置页的候选值（``language_choices``）和界面上的展示名
（``language_label``）都会自动跟上；翻译文件按
``resources/i18n/albuswall_<code>.qm`` 命名。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "Language",
    "language_choices",
    "language_label",
]

#: 默认界面语言：中文（设置页可切换到英文）。
#: 同时也是「没有可用翻译 / 没有配置」时的回落语言。
DEFAULT_LANGUAGE = "zh_CN"


@dataclass(frozen=True, slots=True)
class Language:
    """一个可选语言。

    ``native_name`` 用**该语言自己的写法**（English / 简体中文），
    因此在任何界面语言下都不需要再翻译一次。
    """

    code: str
    native_name: str


#: 界面语言候选（顺序即设置页下拉框顺序，默认语言排最前）。
LANGUAGES: tuple[Language, ...] = (
    Language("zh_CN", "简体中文"),
    Language("en_US", "English"),
)

_LANGUAGE_BY_CODE: dict[str, Language] = {
    language.code: language for language in LANGUAGES
}


def language_choices() -> tuple[str, ...]:
    """设置页下拉框的候选值（语言代码）。"""
    return tuple(language.code for language in LANGUAGES)


def language_label(code: object) -> str:
    """语言代码 → 展示名；未知代码原样返回，方便排查配置。"""
    text = str(code or "").strip()
    if not text:
        return ""
    language = _LANGUAGE_BY_CODE.get(text)
    return language.native_name if language is not None else text
