#
""""""

from albuswall.core import Container

from .database import register_database
from .task import register_task_service
from .trigger import register_trigger_service
from .reconcile import register_reconciler

_REG_MAP = {
    "database": register_database,
    "task": register_task_service,
    "trigger": register_trigger_service,
    "reconcile": register_reconciler,
}


def registry_infrastructure(container: Container):
    for factory in _REG_MAP.values():
        factory(container)
