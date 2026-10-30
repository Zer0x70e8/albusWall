#
"""INI 后端。

格式约定（与读取侧 ``utils.ini_parser`` 对称）：

- 整个文件被 ``RawINIParser`` 读成 ``{(section, key): str}``；没有节头的文件
  归入 ``__default__``。
- 写回走 ``utils.ini_transform.transform``：**逐行**编辑，保留注释 / 顺序 /
  缩进续行 / 原换行符（``\\r\\n`` 或 ``\\n``），再用 ``atomic_write`` 原子落盘。
- ``value is DELETE`` 表示删掉该 key（含续行）；空 section 不自动清理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..registry import ConfigFile
from ..serialization import to_ini
from ..utils.ini_parser import RawINIParser
from ..utils.ini_transform import DELETE, transform  # noqa: F401  (DELETE re-export)
from . import register_backend, resolve_candidates

__all__ = ["INIBackend", "DELETE"]

_SUFFIX = ".ini"


class INIBackend:
    """``.ini`` / ``.conf`` 的读写实现。"""

    name = ConfigFile.INI

    # ---------- 读 ----------
    def read(self, path: Path) -> dict[tuple[str, str], str]:
        path = Path(path)
        if not path.exists():
            return {}
        parser = RawINIParser()
        with path.open("r", encoding="utf-8") as f:
            parser.read_file(f, source=str(path))
        return parser.to_raw_dict()

    # ---------- 写 ----------
    def write(self, path: Path, edits: dict[str, dict[str, Any]]) -> None:
        """按 origin 分组后的 edits 写回单个文件（值必须是 ``str`` 或 ``DELETE``）。"""
        path = Path(path)

        # 延迟 import：configue 不应在导入期拖起整个 infrastructure 包
        from albuswall.infrastructure.fs import atomic_write

        # 新文件的父目录可能不存在（resolved 之后仍可能被删）→ 先建
        path.parent.mkdir(parents=True, exist_ok=True)

        # newline="" 关闭换行符翻译，才能原样探测并保留 \r\n / \n
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as f:
                original = f.read()
        else:
            original = ""
        eol = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            # transform 追加新 key 时假定每行自带换行，这里补齐最后一行
            lines[-1] += eol

        text_edits: dict[str, dict[str, Any]] = {}
        for section, kv in edits.items():
            for key, value in kv.items():
                if value is not DELETE and not isinstance(value, str):
                    raise TypeError(
                        f"INI backend expects str or DELETE, "
                        f"got {type(value).__name__} for [{section}]{key}"
                    )
                text_edits.setdefault(section, {})[key] = value

        out = "".join(transform(lines, text_edits, eol=eol))
        if not original.strip():
            # transform 在「追加新 section」时会先吐一个空行；文件本来是空的
            # （新建）时这个前导空行没有意义，清掉。
            out = out.lstrip("\r\n")

        def _write(tmp: Path) -> None:
            with tmp.open("w", encoding="utf-8", newline="") as f:
                f.write(out)

        atomic_write(path, _write)

    # ---------- 路径解析 ----------
    def resolve(self, base: Path) -> Path:
        """``config`` / ``config.conf`` 这类写法允许补全成 ``config.ini``。"""
        return resolve_candidates(base, _SUFFIX)

    # ---------- 序列化 ----------
    def serialize(
            self,
            value: Any,
            converter: Any = None,
            custom_converters: Optional[Mapping[Any, Callable[[str], Any]]] = None,
            custom_serializers: Optional[Mapping[Any, Callable[[Any], str]]] = None,
    ) -> str:
        """typed value → INI 文本（与 ``_convert`` 读写对称）。"""
        return to_ini(value, converter, custom_converters, custom_serializers)


register_backend(INIBackend())
