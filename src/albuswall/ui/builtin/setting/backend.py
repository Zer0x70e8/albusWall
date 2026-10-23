#
"""设置页的数据后端：读结构 / 写回值。

这一层是「设置页」和「配置系统」之间唯一的接缝，两个方向都复用现成的实现：

* **读**：``services.command.register_user_settings()`` 登记设置树，
  ``configue.setting_schema.dump_setting_schema()`` 导出成 dict ——
  和 ``albusctl setting.schema --view tree`` 拿到的是**同一份数据**，
  UI 因此不需要认识任何 ``ConfigField``。
* **写**：``save_setting_schema()`` / ``save_setting_group()``，和
  ``albusctl setting.set`` 走同一条「按归属文件分组 → flush」的落盘路径。

为什么不用 ``Dispatcher``
-------------------------
``Dispatcher.call`` 会把 handler 排到主循环线程再同步等结果，而设置页本身
就跑在主循环线程（GUI 线程）上 —— 在属主线程里等自己只会死锁。UI 直接调
同一批函数既安全又省一层序列化。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from albuswall.log import getLogger

__all__ = ["SettingBackend"]

_logger = getLogger(__name__)


class SettingBackend:
    """设置结构的读写入口（阻塞，只在主线程用）。"""

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def load_schema(self) -> dict[str, Any]:
        """导出整棵设置树（与 ``setting.schema`` 的 tree 视图同构）。

        任何一步失败都不抛：设置页顶多空着，不该拖垮整个窗口。
        """
        try:
            from albuswall.services.command import register_user_settings

            register_user_settings()

            from albuswall.configue.setting_schema import dump_setting_schema

            schema = dump_setting_schema()
        except Exception:  # noqa: BLE001
            _logger.exception("failed to load setting schema for the UI")
            return {
                "groups": [],
                "items": [],
                "by_file": {},
                "unbound": [],
                "summary": {"group_count": 0, "item_count": 0},
            }

        if not isinstance(schema, dict):
            return {"groups": [], "items": [], "summary": {}}
        schema.setdefault("groups", [])
        schema.setdefault("items", [])
        schema.setdefault("summary", {})
        return schema

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def save(
            self,
            values: Mapping[str, Any],
            *,
            group: str | None = None,
            strict: bool = False,
    ) -> dict[str, Any]:
        """写回一组 ``{key: value}``，返回 ``SettingWriteResult.to_dict()``。

        ``key`` 用 ``group.item`` 形式（如 ``appearance.theme``），也可以
        ``[section]key``；值可以给字符串，``SettingItem.coerce`` 会按声明类型
        转换。

        Raises:
            ValueError: 键不存在 / 值转不过去 / 该节点没有归属文件。
        """
        if not values:
            return {"written": [], "flushed": [], "applied": [], "dry_run": False}

        from albuswall.configue.setting_schema import (
            save_setting_group,
            save_setting_schema,
        )

        if group is None:
            result = save_setting_schema(values, strict=strict)
        else:
            result = save_setting_group(group, values, strict=strict)

        payload = result.to_dict()
        payload["ok"] = not payload.get("flushed") or bool(payload.get("written"))
        return payload

    # ------------------------------------------------------------------ #
    # 自省
    # ------------------------------------------------------------------ #
    @staticmethod
    def describe_files(schema: Mapping[str, Any]) -> list[Path]:
        """schema 里出现过的归属文件（用于在 UI 上提示「改哪了」）。"""
        by_file = schema.get("by_file") or {}
        if not isinstance(by_file, Mapping):
            return []
        return [Path(str(path)) for path in by_file]
