#
"""后端抽象：``Backend`` 协议 + 注册表 + 共享的候选路径解析。

设计约定（新增后端时照抄这四条即可）：

1. **``read(path)``** 返回 ``{(section, key): value}``。value 可以是 ``str``
   （INI）或 typed（JSON / TOML）。空文件 → ``{}``。错误统一抛
   ``ConfigError``（``load()`` 会包成 ``LoadError(kind="read_failed")``）。
2. **``write(path, edits)``** 的 ``edits`` 是 ``{section: {key: value_or_DELETE}}``，
   其中 ``value`` 已经是**可直接落盘的形态**（INI 是 ``str``，JSON 是 JSON
   原生类型），``DELETE`` 表示删除。原子写、父目录自动创建。
3. **``serialize(value, converter, ...)``** 负责 typed → 可写形态。**后端不做
   类型转换语义**，只做形态转换；typed → 文本/原生 的规则在
   ``serialization.py`` 与后端各自的 docstring 里。
4. **``resolve(base)``**：``base`` 已 ``.resolve()``，按后端自己的后缀规则挑出
   唯一存在的文件；0 个 → ``ConfigFileNotFound``，多个 → ``AmbiguousConfigFile``。

职责分离一句话：**``Configue.flush`` 决定「写哪个文件的哪个 key」，后端只决定
「这个值在这个格式里长什么样、这一行/这一个字段怎么写」**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from ..errors import (
    AmbiguousConfigFile,
    ConfigError,
    ConfigFileNotFound,
)
from ..registry import ConfigFile

__all__ = [
    "Backend",
    "register_backend",
    "get_backend",
    "registered_backends",
    "resolve_candidates",
]


@runtime_checkable
class Backend(Protocol):
    """一个配置文件格式的读写适配器。"""

    #: 该后端对应的 ``ConfigFile`` 成员（同时也是注册表 key）
    name: ConfigFile

    def read(self, path: Path) -> dict[tuple[str, str], Any]:
        """读取文件 → ``{(section, key): value}``；文件不存在 → ``{}``。"""
        ...

    def write(self, path: Path, edits: dict[str, dict[str, Any]]) -> None:
        """把 edits 原子写回；``value is DELETE`` 表示删除该 key。"""
        ...

    def resolve(self, base: Path) -> Path:
        """把 base 解析成唯一存在的文件路径。"""
        ...

    def serialize(
            self,
            value: Any,
            converter: Any = None,
            custom_converters: Optional[Mapping[Any, Callable[[str], Any]]] = None,
            custom_serializers: Optional[Mapping[Any, Callable[[Any], str]]] = None,
    ) -> Any:
        """typed value → ``write()`` 能接受的形态。"""
        ...


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_BACKENDS: dict[ConfigFile, Backend] = {}


def register_backend(backend: Backend) -> None:
    """登记一个后端（同名覆盖）。内置后端在各自模块导入期自调用。"""
    _BACKENDS[backend.name] = backend


def get_backend(f: ConfigFile) -> Backend:
    """取后端实例；未注册 → ``ConfigError``（TOML / YAML 就是这条路径）。"""
    backend = _BACKENDS.get(f)
    if backend is None:
        raise ConfigError(f"no backend registered for {f!r}")
    return backend


def registered_backends() -> dict[ConfigFile, Backend]:
    """已注册后端的副本（调试 / 文档用）。"""
    return dict(_BACKENDS)


# ---------------------------------------------------------------------------
# 共享的候选路径解析（INI / JSON 只差一个后缀）
# ---------------------------------------------------------------------------

def resolve_candidates(base: Path, suffix: str) -> Path:
    """按「原路径 → ``<name><suffix>`` → ``<stem><suffix>``」挑唯一存在的文件。

    ``base`` 必须是绝对路径（调用方已 ``resolve()``/拼接好）。候选去重后
    依次判 ``exists()``：0 个 → ``ConfigFileNotFound``，多个 →
    ``AmbiguousConfigFile``（不猜，让调用方改名或写绝对路径）。

    ``base`` 已经带目标后缀时（``state.json``），额外把「再补一次后缀」的形态
    （``state.json.json``）也算作候选：这种文件名通常是手滑产物，静默选中前者
    会让改动莫名其妙地不生效，报歧义更安全。
    """
    base = Path(base)
    candidates: list[Path] = [base]
    if base.suffix.lower() != suffix:
        if base.suffix:
            candidates.append(base.with_suffix(base.suffix + suffix))
        else:
            candidates.append(base.with_suffix(suffix))
        candidates.append(base.parent / (base.stem + suffix))
    else:
        candidates.append(Path(str(base) + suffix))

    unique: list[Path] = []
    for cand in candidates:
        if cand not in unique:
            unique.append(cand)

    found = [c for c in unique if c.exists()]
    if not found:
        raise ConfigFileNotFound(
            f"config file not found: {base} (tried {[str(c) for c in unique]})"
        )
    if len(found) > 1:
        raise AmbiguousConfigFile(
            f"ambiguous file spec {base}: multiple candidates exist: "
            f"{[str(c) for c in found]}"
        )
    return found[0]


# 内置后端：导入即自注册（放在文件末尾，此时本模块的名字都已就绪）
from . import ini as _ini  # noqa: E402,F401
from . import json as _json  # noqa: E402,F401
