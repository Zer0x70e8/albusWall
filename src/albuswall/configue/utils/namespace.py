#
"""
Module providing Namespace and FrozenNamespace classes.

Namespace allows dot-notation access to nested dictionaries, automatically converting
dict values into sub-namespaces. FrozenNamespace is an immutable variant that can be
frozen to prevent further modifications.
"""

from typing import Any, Dict, TYPE_CHECKING
from collections.abc import Mapping

__all__ = ["Namespace", "FrozenNamespace"]


class Namespace:
    """A namespace with dot-notation access that converts dict values into nested namespaces.

    Supports len(), iteration, membership testing, get(), update(), copy(),
    and dict-like views. Equality is checked by comparing the dict representations.
    """

    def __init__(self, **entries: Any) -> None:
        """Initialize with keyword arguments. Plain dict values become nested namespaces."""
        for key, value in entries.items():
            if isinstance(value, dict):
                value = self.__class__(**value)
            self.__setattr__(key, value)

    def __setattr__(self, key: str, value: Any) -> None:
        """Set an attribute. Dicts are promoted to sub-namespaces."""
        if isinstance(value, dict):
            value = self.__class__(**value)
        super().__setattr__(key, value)

    def __delattr__(self, key: str) -> None:
        """Delete an attribute."""
        super().__delattr__(key)

    def __setitem__(self, key, value):
        # self.__dict__[key] = value
        setattr(self, key, value)

    def __getitem__(self, key):
        return self.__dict__[key]

    def __repr__(self) -> str:
        """Return a string showing the class name and attributes."""
        return f"{self.__class__.__name__}({self.__dict__})"

    def to_dict(self) -> Dict[str, Any]:
        """Recursively convert the namespace (and nested namespaces) to a plain dict."""
        result: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Namespace):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result

    def __iter__(self):
        """Iterate over the attribute names (keys)."""
        return iter(self.__dict__)

    def __len__(self) -> int:
        """Return the number of attributes."""
        return len(self.__dict__)

    def __contains__(self, key: str) -> bool:
        """Return True if the key exists as an attribute."""
        return key in self.__dict__

    def get(self, key: str, default: Any = None) -> Any:
        if not isinstance(self, Namespace):
            raise TypeError(
                f"get() called on {type(self).__name__!r}. "
                "Expected a Namespace instance."
            )
        return self.__dict__.get(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Update attributes from a mapping/iterable and/or keyword arguments.

        Values that are plain dicts will become nested namespaces.
        Raises TypeError if more than one positional argument is given.
        """
        if len(args) > 1:
            raise TypeError(
                f"update expected at most 1 positional argument, got {len(args)}"
            )
        if args:
            other = args[0]
            if isinstance(other, Mapping):
                for key, value in other.items():
                    self.__setattr__(key, value)
            else:
                for key, value in other:
                    self.__setattr__(key, value)
        for key, value in kwargs.items():
            self.__setattr__(key, value)

    def subtree(self, path: str) -> "Namespace":
        node = self
        for part in path.split("."):
            nxt = node.get(part)
            if not isinstance(nxt, self.__class__):
                nxt = self.__class__()
                # nxt._configue_ref = getattr(self, "_configue_ref", None)
                # nxt._logger = getattr(self, "_logger", None)
                setattr(node, part, nxt)
            node = nxt
        return node

    def ensure(self, key: str, default: str):
        if key not in self.keys():
            setattr(self, key, default)

    def keys(self):
        """Return a view of the keys (attribute names)."""
        return self.__dict__.keys()

    def values(self):
        """Return a view of the values."""
        return self.__dict__.values()

    def items(self):
        """Return a view of the key-value pairs."""
        return self.__dict__.items()

    def __eq__(self, other: Any) -> bool:
        """Compare two Namespace objects by converting them to dicts."""
        if isinstance(other, Namespace):
            return self.to_dict() == other.to_dict()
        return NotImplemented

    def copy(self) -> "Namespace":
        """Return a shallow copy (nested namespaces are shared)."""
        cls = self.__class__
        new = cls.__new__(cls)
        new.__dict__.update(self.__dict__)
        return new


class FrozenNamespace(Namespace):
    """An immutable namespace. Once frozen, setting or deleting attributes raises AttributeError."""

    # Mangled attribute name for the frozen flag
    _FROZEN_FLAG = '_FrozenNamespace__frozen'
    _INTERNAL_KEYS = frozenset({_FROZEN_FLAG})

    def __init__(self, **entries: Any) -> None:
        """Create a frozen namespace.

        If keyword arguments are given, the instance is frozen immediately.
        Without arguments, it is mutable until the frozen() method is called.
        """
        # Initialize as unfrozen (bypass __setattr__)
        super().__setattr__(self._FROZEN_FLAG, False)

        if entries:
            # __frozen is False, so parent __init__ can write attributes
            super().__init__(**entries)
            # Freeze immediately
            super().__setattr__(self._FROZEN_FLAG, True)

        if TYPE_CHECKING:
            self.__frozen: bool = True  # Only for IDE type checking

    def __setattr__(self, key: str, value: Any) -> None:
        """Raise AttributeError if the namespace is frozen."""
        if getattr(self, self._FROZEN_FLAG, False):
            raise AttributeError("Static configuration cannot be modified")
        super().__setattr__(key, value)

    def __delattr__(self, key: str) -> None:
        """Raise AttributeError if the namespace is frozen."""
        if getattr(self, self._FROZEN_FLAG, False):
            raise AttributeError("Static configuration cannot be deleted")
        super().__delattr__(key)

    # Override base class methods to filter internal attributes
    def to_dict(self) -> Dict[str, Any]:
        """Recursively convert to a plain dict, excluding internal keys."""
        result: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key in self._INTERNAL_KEYS:
                continue
            if isinstance(value, Namespace):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result

    def __iter__(self):
        return (k for k in self.__dict__ if k not in self._INTERNAL_KEYS)

    def keys(self):
        """Return an iterator over the public attribute names."""
        return (k for k in self.__dict__ if k not in self._INTERNAL_KEYS)

    def values(self):
        return (v for k, v in self.__dict__.items() if k not in self._INTERNAL_KEYS)

    def items(self):
        return ((k, v) for k, v in self.__dict__.items() if k not in self._INTERNAL_KEYS)

    # Copy and freezing
    def copy(self) -> "FrozenNamespace":
        """Shallow copy. The new instance is unfrozen, nested namespaces keep their state."""
        cls = self.__class__
        new = cls.__new__(cls)
        # Copy all attributes except the frozen flag
        for key, value in self.__dict__.items():
            if key != self._FROZEN_FLAG:
                new.__dict__[key] = value
        # Mark the new instance as unfrozen (bypass __setattr__)
        super(FrozenNamespace, new).__setattr__(self._FROZEN_FLAG, False)
        return new

    def frozen(self):
        """Make the namespace immutable. Raises AttributeError if already frozen."""
        if getattr(self, self._FROZEN_FLAG, False):
            raise AttributeError("Namespace is already frozen")
        super().__setattr__(self._FROZEN_FLAG, True)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.to_dict()})"
