#
"""JSON 后端。

文件形状固定为「两层」::

    {
      "core": {"debug": true, "port": 1234},
      "recent": {"paths": ["/a.png", "/b.png"]}
    }

即 **顶层对象 = section，二级对象 = key → 值**。没有注释、没有顺序语义，所以
写回是「读全量 → 改 → 整体写回」（``ensure_ascii=False``、``indent=2``、
``sort_keys=True``：键序稳定，diff 才不会满屏噪音）。

与 INI 的差异（写回时最容易搞混的两点）：

1. **值是 JSON 原生类型**，不是字符串：``True`` 就写 ``true``，``None`` 写
   ``null``，列表写数组。所以 ``serialize`` 用 ``to_json_native`` 而不是
   ``to_ini``。
2. **空 section 顺手清理**：编辑后 section 变空才删，避免把用户手写的空
   section 也误删（那种情况它不在 edits 里）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..errors import ConfigError
from ..registry import ConfigFile
from ..serialization import to_json_native
from ..utils.ini_transform import DELETE
from . import register_backend, resolve_candidates

__all__ = ["JSONBackend"]

_SUFFIX = ".json"


class JSONBackend:
    """``.json`` 的读写实现。"""

    name = ConfigFile.JSON

    # ---------- 读 ----------
    def read(self, path: Path) -> dict[tuple[str, str], Any]:
        path = Path(path)
        if not path.exists():
            return {}

        data = self._load_object(path)
        out: dict[tuple[str, str], Any] = {}
        for section, body in data.items():
            if not isinstance(body, dict):
                raise ConfigError(
                    f"JSON section {section!r} in {path} must be an object, "
                    f"got {type(body).__name__}"
                )
            for key, value in body.items():
                out[(str(section), str(key))] = value
        return out

    # ---------- 写 ----------
    def write(self, path: Path, edits: dict[str, dict[str, Any]]) -> None:
        path = Path(path)
        existing: dict[str, Any] = (
            self._load_object(path) if path.exists() else {}
        )

        for section, kv in edits.items():
            body = existing.get(section)
            if body is None:
                body = existing[section] = {}
            elif not isinstance(body, dict):
                raise ConfigError(
                    f"JSON section {section!r} in {path} is not an object"
                )

            for key, value in kv.items():
                if value is DELETE:
                    body.pop(key, None)
                else:
                    body[key] = value

            # 只清理「本次编辑过且编辑后为空」的 section
            if not body:
                existing.pop(section, None)

        # 延迟 import：configue 不应在导入期拖起整个 infrastructure 包
        from albuswall.infrastructure.fs import atomic_write

        path.parent.mkdir(parents=True, exist_ok=True)

        def _write(tmp: Path) -> None:
            with tmp.open("w", encoding="utf-8", newline="") as f:
                json.dump(
                    existing, f,
                    ensure_ascii=False, indent=2, sort_keys=True,
                )
                f.write("\n")

        atomic_write(path, _write)

    # ---------- 路径解析 ----------
    def resolve(self, base: Path) -> Path:
        return resolve_candidates(base, _SUFFIX)

    # ---------- 序列化 ----------
    def serialize(
            self,
            value: Any,
            converter: Any = None,
            custom_converters: Optional[Mapping[Any, Callable[[str], Any]]] = None,
            custom_serializers: Optional[Mapping[Any, Callable[[Any], str]]] = None,
    ) -> Any:
        """typed value → JSON 原生结构（``None`` 是 ``null``，不是空串）。"""
        return to_json_native(
            value, converter, custom_converters, custom_serializers,
        )

    # ---------- 内部 ----------
    @staticmethod
    def _load_object(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"failed to read {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"JSON root of {path} must be an object, "
                f"got {type(data).__name__}"
            )
        return data


register_backend(JSONBackend())
