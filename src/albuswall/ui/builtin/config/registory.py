#
""""""

from typing import TypeVar

from albuswall.configue.declaration import (
    ConfigDeclaration,
    ConfigField,
)
from albuswall.configue.utils import Namespace

T = TypeVar("T")


class _ScopedDeclaration(ConfigDeclaration):
    """根挂在 dynamic.<scope> 下的 Declaration 基类。"""
    _scope: str = ""

    @classmethod
    def _get_config_namespace(cls) -> Namespace:
        dynamic = cls._config_getter().dynamic
        ns = dynamic.get(cls._scope, None)
        if not isinstance(ns, Namespace):
            ns = Namespace()
            dynamic[cls._scope] = ns
        return ns


class UIDeclaration(_ScopedDeclaration):
    _scope = "ui"


class PreferenceDeclaration(_ScopedDeclaration):
    _scope = "preference"


class WindowStateDeclaration(_ScopedDeclaration):
    _scope = "window"


class UIField(ConfigField[T]):
    _declaration_cls = UIDeclaration


class PreferenceField(ConfigField[T]):
    _declaration_cls = PreferenceDeclaration


class WindowStateField(ConfigField[T]):
    _declaration_cls = WindowStateDeclaration
