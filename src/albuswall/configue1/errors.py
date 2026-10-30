#
"""错误类与加载报告。

分两类：

- **异常**：``ConfigError`` / ``ConfigFileNotFound`` / ``AmbiguousConfigFile`` /
  ``ConfigMissingError``——调用方需要 ``except`` 的那些。
- **值对象**：``LoadError`` / ``LoadReport``——``check()`` / ``load()`` 的返回值，
  「一部分文件成功、一部分失败」是正常结果，不该用异常表达。

``ConfigError`` 与 ``ConfigFileNotFound`` 定义在本模块，``registry`` 只是
re-export（它们既属于声明层，也属于访问层，放这里避免两边各定义一份导致
``except`` 抓不住）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ConfigError",
    "ConfigFileNotFound",
    "AmbiguousConfigFile",
    "ConfigMissingError",
    "LoadError",
    "LoadReport",
]


class ConfigError(Exception):
    """配置错误基类。"""


class ConfigFileNotFound(ConfigError):
    """reg(exists_check=True) 或 load() 时文件不存在。"""


class AmbiguousConfigFile(ConfigError):
    """load() 时多个候选文件同时存在（无法自动选择）。"""


class ConfigMissingError(AttributeError, ConfigError):
    """无值可读：未 load / 无默认且文件未写。

    继承 ``AttributeError`` 是为了兼容 ``getattr(obj, "x", default)`` 语义：
    ``getattr`` 只吞 ``AttributeError``，不吞普通异常。

    ``path`` / ``file`` 是结构化上下文（便于上层做「缺失字段汇总」），
    不参与 ``str()``。
    """

    def __init__(
            self,
            message: str,
            *,
            path: tuple[str, ...] | None = None,
            file: str | None = None,
    ) -> None:
        self.path = path
        self.file = file
        super().__init__(message)


# ---------------------------------------------------------------------------
# 加载报告
# ---------------------------------------------------------------------------

# noinspection SpellCheckingInspection
LoadErrorKind = Literal[
    "unregistered_file",
    "file_not_found",
    "ambiguous_file",
    "read_failed",
    "hook_failed",
    "type_check_failed",
]


@dataclass(frozen=True)
class LoadError:
    """一条加载错误（值对象，不是异常）。"""

    kind: LoadErrorKind
    file: str | None = None
    path: tuple[str, ...] | None = None
    message: str = ""


@dataclass(frozen=True)
class LoadReport:
    """``check()`` / ``load()`` 的结构化结果。

    ``ok`` 只看 ``errors``：``warnings`` 不影响成败。
    """

    loaded_files: tuple[str, ...] = ()
    skipped_files: tuple[str, ...] = ()
    errors: tuple[LoadError, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors
