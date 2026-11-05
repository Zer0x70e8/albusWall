#
"""``field()`` 的 i18n 包装：把设置项的文案变成「dump 时求值的 callable」。

问题
====

``@configclass`` 的 ``field(label=..., description=...)`` 只能写死字符串，
而设置页的文案要跟着界面语言变。翻译必须在**渲染那一刻**发生：
``QTranslator`` 切换后，之前取的字符串不会自己变。

做法
====

``albuswall.configue`` 不认识任何翻译框架（它整层无 Qt），只提供一个 hook：
``label`` / ``description`` 允许是 ``Callable[[], str]``，由 ``dump_schema``
在**求值时**调用。本模块把 i18n key 包进这个 callable：

.. code-block:: python

    from albuswall.ui.config_i18n import field

    @configclass("thumbnail", "main")
    class ThumbnailConfig:
        batch_size: int = field(
            default=32,
            label="thumbnail.batch_size.label",
            description="thumbnail.batch_size.description",
        )

于是：``dump_schema()`` → ``tr(label)`` → 当前语言；切语言后再
``dump_schema()`` 就是新文案（不需要重建 class / 重启）。

为什么在 UI 侧
==============

翻译入口是 Qt 的 ``QCoreApplication.translate(context, text)``（与
:class:`albuswall.ui.delegate.info_cell_delegate.InfoCellDelegate` 同一套
静态上下文写法）。``configue`` 不能 import Qt，所以一层薄包装放在这里。

注意：``field()`` 的参数**照旧**传 ``str``——传进来的是 i18n key（源文），
不是译文；没有对应译文时 Qt 原样返回源文，界面不会变成 key 名。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication

from albuswall.configue import field as _field

__all__ = ["SETTINGS_CONTEXT", "field", "tr"]

#: 设置项文案的翻译上下文（``.ts`` 里的 ``<context><name>``）。
#:
#: 单独一个上下文而不是复用 ``QObject.tr`` 的类名：设置项文案由配置模块
#: 声明、由设置页渲染，两头都不属于某个具体控件类。
SETTINGS_CONTEXT = "Settings"


def tr(text: str) -> str:
    """把一条设置文案（i18n key / 源文）翻译成当前语言。

    没有 ``QCoreApplication``（纯逻辑单测）时直接返回源文——翻译缺失不该
    让 dump 失败，``_resolve_text`` 那边还有一层异常兜底。
    """
    if not text:
        return ""
    if QCoreApplication.instance() is None:
        return str(text)
    try:
        return QCoreApplication.translate(SETTINGS_CONTEXT, str(text))
    except Exception:  # noqa: BLE001 - 翻译失败退回源文
        return str(text)


def field(*, label: Any = "", description: Any = "", **kwargs: Any) -> Any:
    """``configue.field`` 的 i18n 包装：``label`` / ``description`` 视为 i18n key。

    * 传字符串 → 包成 ``lambda: tr(该字符串)``（**dump 时**才翻译）；
    * 传 ``""`` / ``None`` → 原样留空（不包 callable，省一次调用）；
    * 已经是 callable → 原样透传（调用方自己控制求值时机）。

    其余参数（``default=`` 等）原样交给 :func:`albuswall.configue.field`。
    """
    return _field(
        label=_as_text_loader(label),
        description=_as_text_loader(description),
        **kwargs,
    )


def _as_text_loader(value: Any) -> Any:
    """把文案参数收口成 ``""`` / callable（``configue`` 两种都接受）。"""
    if value is None or value == "":
        return ""
    if callable(value):
        return value
    key = str(value)
    return lambda: tr(key)
