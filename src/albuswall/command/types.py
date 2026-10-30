#
""""""

from typing import Callable, Any

Handler = Callable[..., Any]
Authorizer = Callable[[str, frozenset[str]], bool]

_EMPTY: frozenset[str] = frozenset()
