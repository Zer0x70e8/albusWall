#
""""""

from __future__ import annotations

from typing import Protocol, Type, Callable, Dict, runtime_checkable

from albuswall.core.main_loop import MainLoop


@runtime_checkable
class UIProtocol(Protocol):
    """UI 实现只需：搭界面、交出主循环、拆界面。"""

    name: str
    main_loop: MainLoop

    def setup(self, container) -> None:
        """构建 UI（窗口/信号/主题）。不阻塞。"""
        ...

    def teardown(self) -> None:
        """释放资源。在 loop.run() 返回后被调用。"""
        ...

    def __str__(self):
        return "\n".join((
            f"{type(self).__name__}: (",
            f"\tname: {self.name}",
            f"\tmain_loop: {type(self.main_loop).__name__}",
            ")"
        ))


_REGISTRY: Dict[str, Type[UIProtocol]] = {}


def register_ui(name: str, override: bool = False) -> Callable:
    def deco(cls: Type[UIProtocol]) -> Type[UIProtocol]:
        if name in _REGISTRY and not override:
            raise RuntimeError(f"UI '{name}' is registered.")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def get_ui(name: str) -> Type[UIProtocol]:
    if name in _REGISTRY:
        return _REGISTRY[name]
    else:
        raise RuntimeError(f"UI '{name}' is not registered.")


def list_uis() -> list[str]:
    return list(_REGISTRY.keys())
