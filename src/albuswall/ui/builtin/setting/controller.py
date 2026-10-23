#
"""设置页的写入协调器。

把「读结构 / 写回 / 写了之后刷新」的副作用集中到一处，让
:class:`~albuswall.ui.builtin.setting.rows.SettingRow` 只管显示：

* **读**：``backend.load_schema()`` 之后立刻叠上
  :mod:`~albuswall.ui.builtin.setting.overrides` 的补丁，得到「UI 视角」的
  结构；
* **写**：一个值可以对应**多个配置节点**（见 ``overrides.ItemPatch.apply``），
  例如「选主题」要同时写 ``[ui]theme`` / ``[path]theme``；
* **写完刷新**：多键写入后重新 dump 一次结构，把被连带改掉的兄弟行
  （比如样式表路径）就地同步，不打断用户的滚动位置和焦点。
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QObject, Signal

from .backend import SettingBackend
from .overrides import apply_patches

__all__ = ["MISSING", "SettingController"]

#: 「这个 key 不在当前结构里」的哨兵
MISSING = object()


class SettingController(QObject):
    """设置页的读写协调器。"""

    #: 某个 key 写回成功：``(key, result_dict)``
    changed = Signal(str, dict)
    #: 某个 key 写回失败：``(key, message)``
    failed = Signal(str, str)
    #: 整棵结构重新加载完：``(schema_dict)``
    reloaded = Signal(dict)
    #: 值已写回内存：载荷是本次写入的 ``{key: value}``。供「写完之后还有
    #: 旁路动作」的场景使用（例如切语言要重设整个界面的文案）。
    applied = Signal(dict)

    def __init__(
            self,
            backend: SettingBackend | None = None,
            parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend or SettingBackend()
        self._schema: dict[str, Any] = {}
        self._items: dict[str, dict[str, Any]] = {}
        # 行控件登记表：写回之后要能就地刷新「被连带改掉」的行
        self._rows: list[weakref.ref] = []

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> SettingBackend:
        return self._backend

    @property
    def schema(self) -> dict[str, Any]:
        """最近一次加载（并叠过补丁）的结构。"""
        return self._schema

    def load_schema(self) -> dict[str, Any]:
        """后端 dump + 补丁。"""
        return apply_patches(self._backend.load_schema())

    def reload(self) -> dict[str, Any]:
        """重新读一遍结构，并广播 ``reloaded``。"""
        self._adopt(self.load_schema())
        self.reloaded.emit(self._schema)
        return self._schema

    def item(self, key: str) -> dict[str, Any] | None:
        """按 key 取叶子 dict（已叠补丁）。"""
        return self._items.get(key)

    def current_value(self, key: str) -> Any:
        """按 key 取当前值；key 不在结构里时返回 :data:`MISSING`。"""
        item = self._items.get(key)
        return MISSING if item is None else item.get("value")

    def _adopt(self, schema: dict[str, Any]) -> None:
        self._schema = schema
        self._items = {
            str(item.get("key")): item
            for item in schema.get("items") or []
            if isinstance(item, dict) and item.get("key")
        }

    # ------------------------------------------------------------------ #
    # 行登记（写回后刷新用）
    # ------------------------------------------------------------------ #
    def register_row(self, row: Any) -> None:
        self._rows.append(weakref.ref(row))

    def _live_rows(self) -> list[Any]:
        live: list[Any] = []
        kept: list[weakref.ref] = []
        for ref in self._rows:
            row = ref()
            if row is None:
                continue
            kept.append(ref)
            live.append(row)
        self._rows = kept
        return live

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def apply(self, key: str, value: Any) -> dict:
        """写回单个节点（便捷入口）。"""
        return self.apply_many({key: value}, edited=key)

    def apply_many(
            self,
            values: dict[str, Any],
            *,
            edited: str | None = None,
    ) -> dict:
        """写回一组 ``{key: value}``。

        多键写入（``edited`` 之外还有别的键）写完后会静默重新 dump 一次结构，
        并把被连带改掉的行就地刷新 —— 因为「选主题」顺手改了样式表路径，
        界面上那一行必须跟着变。

        Raises:
            Exception: 原样抛出后端的错误（值非法 / 无可写文件 / 写盘失败），
                由调用方（SettingRow）就地展示。
        """
        if not values:
            return {}
        key = edited or next(iter(values))
        try:
            result = self._backend.save(values)
        except Exception as exc:  # noqa: BLE001  交给行内展示
            self.failed.emit(key, str(exc))
            raise
        # 先广播「值已生效」：监听方（如语言切换）可能触发整页重建，
        # 让它发生在行内状态刷新之前，用户仍能看到 ✔ 反馈。
        self.applied.emit(dict(values))
        self.changed.emit(key, result if isinstance(result, dict) else {})

        if len(values) > 1:
            self._sync_rows([k for k in values if k != key])
        return result

    def _sync_rows(self, keys: Iterable[str]) -> None:
        """重新 dump 结构，并让受影响的已建行同步显示。"""
        targets = set(keys)
        if not targets:
            return
        try:
            self._adopt(self.load_schema())
        except Exception:  # noqa: BLE001  刷新失败不该把写回成功也说成失败
            return
        for row in self._live_rows():
            row_key = getattr(row, "key", None)
            if row_key in targets:
                try:
                    row.refresh_from_schema(self._schema)
                except Exception:  # noqa: BLE001
                    continue
