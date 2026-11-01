#
"""执行期异常 re-export。

实现已合并到 ``albuswall.command.exceptions``；本模块仅保留以兼容
``service.command`` 内部的旧 import。下一轮把 ``dispatcher.py`` /
``service.py`` / ``ipc_request_service.py`` 搬进 ``albuswall.command``
后，本文件连同 ``from .exceptions import ...`` 一并删除。
"""

from albuswall.command.exceptions import (
    DispatchError, BadRequest, UnknownCommand,
    CommandNotExecutable, ArgumentBindingError, CommandFailed,
    DispatchTimeout, DispatchUnavailable,
)

__all__ = [
    "DispatchError", "BadRequest", "UnknownCommand",
    "CommandNotExecutable", "ArgumentBindingError", "CommandFailed",
    "DispatchTimeout", "DispatchUnavailable",
]
