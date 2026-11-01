#
"""命令系统异常：注册期 + 执行期。

- 注册期：``CommandRegistryError`` 及子类，由 ``registry.register`` 抛出。
- 执行期：``DispatchError`` 及子类，由 ``Dispatcher.dispatch`` /
  ``CommandService`` 抛出。

合并在一处的理由：两族异常共享 ``AppError`` 基类和 ``ExitCode``，且
``builtin.py``（诊断命令）需要同时引用注册期（``UnknownCommand``）与
不引用执行期，拆两个文件只会让调用方多 import 一行。
"""

from __future__ import annotations

from albuswall.core import AppError, ExitCode

__all__ = [
    # 注册期
    "CommandRegistryError", "DuplicateCommand", "RegistryFrozen",
    "InvalidCommandName",
    # 执行期
    "DispatchError", "BadRequest", "UnknownCommand",
    "CommandNotExecutable", "ArgumentBindingError", "CommandFailed",
    "DispatchTimeout", "DispatchUnavailable",
]


# ==================== 注册期异常 ====================

class CommandRegistryError(AppError):
    """注册表相关异常基类。"""
    code = "registry_error"
    exit_code = ExitCode.CONFIG_ERROR


class DuplicateCommand(CommandRegistryError):
    code = "duplicate_command"

    def __init__(self, name: str) -> None:
        super().__init__(f"duplicate command: {name!r}")
        self.name = name


class RegistryFrozen(CommandRegistryError):
    code = "registry_frozen"

    def __init__(self, name: str) -> None:
        super().__init__(f"registry frozen; cannot register: {name!r}")
        self.name = name


class InvalidCommandName(CommandRegistryError):
    code = "invalid_command_name"

    def __init__(self, name: str) -> None:
        super().__init__(f"invalid command name: {name!r}")
        self.name = name


# ==================== 执行期异常 ====================

class DispatchError(AppError):
    """调度/执行期异常基类。"""
    code = "dispatch_error"
    exit_code = ExitCode.SOFTWARE_ERROR


class BadRequest(DispatchError):
    """请求格式问题（params / method / subject 结构错误）。"""
    code = "bad_request"
    exit_code = ExitCode.USAGE_ERROR


class UnknownCommand(DispatchError):
    """路径或别名未注册。"""
    code = "unknown_command"
    exit_code = ExitCode.USAGE_ERROR

    def __init__(self, path: str) -> None:
        super().__init__(f"unknown command: {path!r}")
        self.path = path


class CommandNotExecutable(DispatchError):
    """命令存在但不可执行（namespace / disabled / acl / authorizer）。"""
    code = "not_executable"
    exit_code = ExitCode.USAGE_ERROR

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"command {path!r} not executable ({reason})")
        self.path = path
        self.reason = reason


class ArgumentBindingError(DispatchError):
    """参数与 Param schema 不匹配。"""
    code = "bad_args"
    exit_code = ExitCode.USAGE_ERROR


class CommandFailed(DispatchError):
    """命令 handler 内部抛了非 DispatchError 异常。"""
    code = "command_error"
    exit_code = ExitCode.SOFTWARE_ERROR

    def __init__(self, path: str, original: BaseException) -> None:
        super().__init__(
            f"command {path!r} raised {type(original).__name__}: {original}"
        )
        self.path = path
        self.original = original


class DispatchTimeout(DispatchError):
    """主循环在超时内没有给出响应（大概率被某个回调阻塞）。

    由服务层 ``MainThreadTimeout`` 经 ``service/command/dispatch_errors.py``
    翻译而来 —— 命令本身没问题，重试通常能成功，所以是软件错误而非用法错误。
    """
    code = "dispatch_timeout"
    exit_code = ExitCode.SOFTWARE_ERROR

    def __init__(
            self,
            message: str = "",
            *,
            original: BaseException | None = None,
    ) -> None:
        #: 原始异常（翻译链上游的服务层异常），供日志 / 诊断回溯。
        self.original = original
        super().__init__(message)


class DispatchUnavailable(DispatchError):
    """调度目标不可用：主循环已关闭 / 退出 / 无法投递。

    由服务层 ``MainLoopUnavailable`` 翻译而来。调用方（``albusctl``）应把它
    理解为"实例正在退出"，而不是命令写错了。
    """
    code = "dispatch_unavailable"
    exit_code = ExitCode.SOFTWARE_ERROR

    def __init__(
            self,
            message: str = "",
            *,
            original: BaseException | None = None,
    ) -> None:
        #: 原始异常（翻译链上游的服务层异常），供日志 / 诊断回溯。
        self.original = original
        super().__init__(message)
