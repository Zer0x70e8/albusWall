#
""""""

from typing import TYPE_CHECKING

from .exceptions import (
    CommandRegistryError, DuplicateCommand, RegistryFrozen, InvalidCommandName,
)
from .types import Authorizer, Handler, _EMPTY
from .model import Param, Command, ExecutionVerdict
from .registry import (
    CommandRegistry, resolve, get_default_registry, list_commands, command, _default,
)

if TYPE_CHECKING:
    from albuswall.core import Container


def registry_command_registry(container: "Container"):
    container.register(
        "command_registry",
        get_default_registry,
        returns=CommandRegistry
    )
