#
"""``[shortcuts]`` 段的**配置值类型**：``ShortcutRecord``（纯标准库，无 Qt）。

为什么放在 ``common``
=====================

这份类型要被两边同时用到，而它们的依赖方向相反：

* ``albuswall.configue.bootstrap`` 在 ``setup_config`` 里把 ``shortcuts.ini``
  登记成两个配置文件，并需要「这个段的每个 key 是什么类型」才能让
  ``load()`` 的类型自检跑得起来；
* ``albuswall.ui.keymap`` 在运行期把记录绑成 Qt 键位（``KeySequence``）。

``configue`` 整层**禁止 import Qt / 任何 UI 包**，而 ``albuswall.ui.keymap``
是 UI 包（它的 ``__init__`` 会拉 ``PySide6.QtWidgets``）。所以两边只能共享一个
**谁都不依赖 Qt** 的模块：``common`` 是 ``configue`` / ``ui`` 的公共上游
（依赖方向 ``common ← (configue / ui)``，与 ``common/constants/ui.py`` 同一约定）。

因此本模块只做「文本 ⇄ 记录」的解析与格式化，**不校验键位语法**（那是
``ui.keymap.sequence.parse_sequences`` 的活，需要 Qt）；它保证的是：结构合法、
键位是字符串、``args`` 是数组、``kwargs`` 是对象。

文件格式（与 ``resources/config/shortcuts.ini`` 顶部注释逐条对应）
================================================================

::

    [shortcuts]
    app.quit        = ["Ctrl+Q"]                              # 简写：键位数组
    view.fullscreen = ["F11", "Ctrl+Shift+F"]
    window.album    = []                                      # 显式解绑
    album.open      = ["Alt+1", [1], {"mode": "fit"}]          # 完整三元组
    app.settings    = "Ctrl+K, Ctrl+S"                        # 单个键位（和弦）

四种写法都归一到 :class:`ShortcutRecord` 的三个字段：

============================  ==================================================
写法                           解释
============================  ==================================================
``[]`` / 空串                   ``keys=()``（显式解绑：功能还在，只是没键）
``"Ctrl+Q"``                    ``keys=("Ctrl+Q",)``
``["F11", "Ctrl+Shift+F"]``     ``keys=("F11", "Ctrl+Shift+F")``（多个键位）
``[keys, args, kwargs]``        完整记录；``keys`` 可为字符串或字符串数组
============================  ==================================================

``to_ini()`` 永远写**完整三元组**（``save()`` 的规范形态），读回时四种都认。
非法 JSON / 结构不对一律 ``ValueError`` —— 配置层会把它包成「读不出来的值」，
写盘失败时保留 dirty，不会把坏数据写进文件。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_NAME",
    "SECTION",
    "ShortcutRecord",
    "type_shortcut_keys",
]

#: 快捷键表所在的段名（``[shortcuts]``）。
SECTION = "shortcuts"

#: 快捷键文件名（随包默认表与用户覆盖表同名，靠目录区分）。
FILE_NAME = "shortcuts.ini"


@dataclass(frozen=True)
class ShortcutRecord:
    """一条快捷键绑定记录：键位 + 触发时的固定实参。

    与 :class:`albuswall.ui.keymap.model.ShortcutBinding` 的区别只有一个：
    ``keys`` 是**原始字符串**（``"Ctrl+SHIFT+P"`` 这种写法原样保留），
    转成 Qt 的 ``KeySequence`` 是 UI 层绑定时的事。这样本模块不依赖 Qt。

    Attributes:
        keys: 会触发该命令的键位文本（0 个 = 显式解绑）。
        args: 触发时按位置传入的固定实参。
        kwargs: 触发时按关键字传入的固定实参。
    """

    keys: tuple[str, ...] = ()
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.keys, tuple):
            object.__setattr__(self, "keys", tuple(self.keys))
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))
        if not isinstance(self.kwargs, Mapping):
            raise TypeError(
                f"ShortcutRecord.kwargs must be a mapping, "
                f"got {type(self.kwargs).__name__}"
            )
        for name in self.kwargs:
            if not isinstance(name, str):
                raise TypeError(
                    f"ShortcutRecord.kwargs keys must be strings: {name!r}"
                )

    # ------------------------------------------------------------------
    # 派生视图
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """是否至少有一个键位（空 = 显式解绑，但命令本身仍然存在）。"""
        return bool(self.keys)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def from_ini(cls, raw: Any) -> "ShortcutRecord":
        """把 ``shortcuts.ini`` 里的一个值解析成记录。

        Args:
            raw: 配置值。``None`` / 空白 / ``[]`` 都表示「没有键位」。

        Raises:
            ValueError: JSON 坏了、结构不是三元组、键位不是字符串等。
        """
        if raw is None:
            return cls()
        if not isinstance(raw, str):
            # JSON 后端（假如将来把快捷键表换成 JSON）会直接给 list/dict
            return cls._from_data(raw)

        text = raw.strip()
        if not text:
            return cls()
        if text.startswith('"'):
            # 单个键位的 JSON 字符串写法："Ctrl+Q" / "G, G"
            try:
                single = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON key record: {raw!r}") from exc
            if not isinstance(single, str):
                raise ValueError(
                    f"key record must be a string when quoted: {raw!r}"
                )
            return cls(keys=(single,))
        if not text.startswith("["):
            # 裸写单个键位（INI 风格，不需要引号）：Ctrl+Q
            return cls(keys=(text,))

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON key record: {raw!r}") from exc
        return cls._from_data(data, raw=raw)

    @classmethod
    def _from_data(cls, data: Any, *, raw: Any = None) -> "ShortcutRecord":
        """把已解析的 JSON 结构（或 JSON 后端给的原生值）转成记录。"""
        shown = repr(raw) if raw is not None else repr(data)
        if not isinstance(data, list):
            raise ValueError(f"key record must be a JSON array: {shown}")

        # 简写：扁平字符串数组 = 多个键位 + 无参数（也覆盖空数组 []）。
        if all(isinstance(item, str) for item in data):
            return cls(keys=_keys_of(data, shown))

        if len(data) != 3:
            raise ValueError(
                f"key record must be [keys, args, kwargs]: {shown}"
            )
        raw_keys, raw_args, raw_kwargs = data
        keys = _keys_of(raw_keys, shown)
        if not isinstance(raw_args, list):
            raise ValueError(
                f"key record positional args must be a JSON array: {shown}"
            )
        if not isinstance(raw_kwargs, dict) or any(
                not isinstance(name, str) for name in raw_kwargs
        ):
            raise ValueError(
                f"key record keyword args must be a JSON object: {shown}"
            )
        return cls(
            keys=keys,
            args=tuple(raw_args),
            kwargs=dict(raw_kwargs),
        )

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------
    def to_ini(self) -> str:
        """写回 ``shortcuts.ini`` 的文本（规范化三元组形态）。

        ``json.dumps`` 保证与 :meth:`from_ini` 往返一致；``ensure_ascii=False``
        让中文参数值在文件里保持可读。不可序列化的 ``args`` / ``kwargs``
        会抛 ``TypeError``（由调用方包成写盘错误，节点保留 dirty）。
        """
        first: str | list[str] = (
            self.keys[0] if len(self.keys) == 1 else list(self.keys)
        )
        payload = [first, list(self.args), dict(self.kwargs)]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def to_binding(self) -> tuple[tuple[str, ...], tuple[Any, ...], dict[str, Any]]:
        """``(keys, args, kwargs)`` 三元组（UI 层据此构造 ``ShortcutBinding``）。"""
        return tuple(self.keys), tuple(self.args), dict(self.kwargs)

    def __str__(self) -> str:  # pragma: no cover - 调试可读性
        return self.to_ini()


def _keys_of(value: Any, shown: str) -> tuple[str, ...]:
    """键位入参 → ``tuple[str, ...]``（单个字符串或字符串数组）。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        items = list(value)
        if not all(isinstance(item, str) for item in items):
            raise ValueError(
                f"key record keys must be strings: {shown}"
            )
        return tuple(items)
    raise ValueError(
        f"key record keys must be a string or an array of strings: {shown}"
    )


def type_shortcut_keys(resolver: Any) -> None:
    """加载钩子：给 ``[shortcuts]`` 段的 key 补上 ``ShortcutRecord`` 类型。

    ``[shortcuts]`` 的 key 是命令路径，只有运行期（插件登记槽之后）才知道，
    写不出 ``@configclass`` 字段，所以只能靠 ``register_load_hook`` 在
    ``load()`` 的 Step 4.5（类型自检之前）批量补类型。

    **只给解析得通的 key 补类型**：补了类型意味着 ``load()`` 的 ``check_all``
    会去转换它，一条手滑的坏记录（少个括号、`args` 不是数组）就会让整个
    启动 ``ConfigError``。坏 key 跳过不登记，于是它在运行期是「没有类型的
    原始字符串」——``ShortcutRegistry`` 读到坏值只记 warning 并跳过该命令
    （退回槽的兜底键位），与改造前「坏条目只跳过这一条」的行为一致。

    幂等：``register_type`` 本身幂等，``load()`` 每次重跑都安全。

    本函数只依赖 ``resolver`` 的三个公开方法（``keys_of`` / ``get_str`` /
    ``register_type``），因此放在 ``common`` 里不需要 import ``configue``。
    """
    for key in resolver.keys_of(SECTION):
        try:
            ShortcutRecord.from_ini(resolver.get_str(SECTION, key))
        except Exception as exc:  # noqa: BLE001 - 坏条目只跳过它自己
            logger.warning(
                "shortcut record [%s]%s is not usable (%s); "
                "it will be ignored at bind time",
                SECTION, key, exc,
            )
            continue
        resolver.register_type(SECTION, key, ShortcutRecord)
