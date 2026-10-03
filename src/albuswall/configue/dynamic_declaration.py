#
"""DynamicDeclaration: 装饰器式登记动态配置子树。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .state import StatefulNamespace

__all__ = [
    "DynamicMountSpec",
    "dynamic_state",
    "DYNAMIC_REGISTRY",
    "reset_registry",
]


@dataclass(frozen=True)
class DynamicMountSpec:
    name: str
    cls: type[StatefulNamespace]
    base_key: tuple[str, ...]  # 相对 StaticConfig 的路径，例如 ("path", "cache")
    filename: str  # 文件名，例如 "user.json"
    autosave: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)

    def resolve_path(self, static) -> Path:
        node = static
        for part in self.base_key:
            node = getattr(node, part)
        return Path(node) / self.filename


# name -> spec
DYNAMIC_REGISTRY: dict[str, DynamicMountSpec] = {}


def dynamic_state(
        name: str,
        base_key: Sequence[str],
        filename: str,
        *,
        autosave: bool = True,
        replace: bool = False,
        **overrides: Any,
):
    """把 StatefulNamespace 子类登记为动态子树。

    用法::

        @dynamic_state("user", ("path", "cache"), "user.json", autosave=True)
        class UserState(StatefulNamespace):
            user_id = StateField(str, default="anon")

    参数：
      name        : 挂载名，对应 _config.dynamic.<name>
      base_key    : 相对 StaticConfig 的目录路径，例如 ("path", "cache")
      filename    : JSON 文件名
      autosave    : 修改时是否自动落盘
      replace     : 重名时是否覆盖，默认 False 抛错
      overrides   : 透传给 cls.load 的额外关键字，会覆盖文件里的值
    """

    def deco(cls: type[StatefulNamespace]):
        if not issubclass(cls, StatefulNamespace):
            # noinspection string-conversion-without-dunder-method
            raise TypeError(
                f"@dynamic_state({name!r}): expected StatefulNamespace "
                f"subclass, got {cls!r}"
            )
        if name in DYNAMIC_REGISTRY and not replace:
            raise RuntimeError(f"dynamic state {name!r} already registered")
        DYNAMIC_REGISTRY[name] = DynamicMountSpec(
            name=name,
            cls=cls,
            base_key=tuple(base_key),
            filename=filename,
            autosave=autosave,
            overrides=dict(overrides),
        )
        return cls

    return deco


def reset_registry() -> None:
    """测试用：清空登记表。"""
    DYNAMIC_REGISTRY.clear()
