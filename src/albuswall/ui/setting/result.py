"""写回报告的形状（``SettingView.show_submit_result`` 认的那一份）。

写回结果有 11 个字段，原来在三个地方各写了一遍：真实写回（configue 的
``save_values``）、presenter 的「出错 / 重试」分支、演示的假后端。形状一旦
加字段（例如后来的 ``deferred`` / ``flushed``），三处都要改，漏一处就是
"某个分支渲染不出来"。

现在只有这一个构造器：谁产报告谁传自己知道的字段，其余走默认值。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["DEFAULT_KEYS", "write_back_result"]

#: 报告的全部字段与默认值（顺序即文档顺序）。
DEFAULT_KEYS: dict[str, Any] = {
    "written": (),
    "by_file": {},
    "flushed": (),
    "applied": (),
    "nodes": (),
    "pending": (),
    "changes": (),
    "validation_errors": (),
    "dry_run": False,
    "deferred": False,
    "ok": True,
    "error": None,
}


def write_back_result(**fields: Any) -> dict[str, Any]:
    """补全一次写回报告（未知字段原样透传，缺省字段走默认值）。"""
    result = {key: value for key, value in DEFAULT_KEYS.items()}
    result.update(fields)
    # 统一成"可 JSON 的普通容器"，避免上游传 tuple / frozen dict 时视图侧行为不一致
    for key in ("written", "flushed", "applied", "nodes", "pending",
                "changes", "validation_errors"):
        result[key] = list(result.get(key) or ())
    by_file = result.get("by_file") or {}
    result["by_file"] = (
        {str(path): list(keys) for path, keys in by_file.items()}
        if isinstance(by_file, Mapping) else {}
    )
    result["ok"] = bool(result.get("ok"))
    result["dry_run"] = bool(result.get("dry_run"))
    result["deferred"] = bool(result.get("deferred"))
    error = result.get("error")
    result["error"] = str(error) if error else None
    return result
