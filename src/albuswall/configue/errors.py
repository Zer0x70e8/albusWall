#
"""错误类与加载报告。

分两类：

- **异常**：``ConfigError`` / ``ConfigFileNotFound`` / ``AmbiguousConfigFile`` /
  ``ConfigMissingError`` / ``ConfigClassNotRegistered`` /
  ``ConfigDeclarationConflict`` / ``ConfigValidationError``——调用方需要
  ``except`` 的那些。
- **值对象**：``LoadError`` / ``LoadReport``——``check()`` / ``load()`` 的返回值，
  「一部分文件成功、一部分失败」是正常结果，不该用异常表达。

``ConfigError`` 与 ``ConfigFileNotFound`` 定义在本模块，``registry`` 只是
re-export（它们既属于声明层，也属于访问层，放这里避免两边各定义一份导致
``except`` 抓不住）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

__all__ = [
    "ConfigError",
    "ConfigFileNotFound",
    "AmbiguousConfigFile",
    "ConfigClassNotRegistered",
    "ConfigDeclarationConflict",
    "ConfigMissingError",
    "ConfigValidationError",
    "LoadError",
    "LoadReport",
]


class ConfigError(Exception):
    """配置错误基类。"""


class ConfigFileNotFound(ConfigError):
    """reg(exists_check=True) 或 load() 时文件不存在。"""


class AmbiguousConfigFile(ConfigError):
    """load() 时多个候选文件同时存在（无法自动选择）。"""


class ConfigValidationError(ConfigError):
    """字段级校验失败（``field(validators=[...])`` 的校验器抛出的错误）。

    与 ``ValueError`` **刻意分开**：

    * ``ValueError`` = **类型转换**失败（coerce 转不过去，写进文件也读不回来）；
    * ``ConfigValidationError`` = 类型对了，但**语义**不合法
      （``port = 99999``、路径不存在……）。

    上层（设置 GUI / CLI）据此给不同提示：前者是「输入格式错」，后者是
    「值不合法」。消息文案可以是 ``str``，也可以是 ``Callable[[], str]``
    （翻译延迟到抛出时求值，与 ``label`` / ``description`` 的约定一致）。
    """

    def __init__(
            self,
            message: str | Callable[[], str],
            *,
            path: str | None = None,
    ) -> None:
        self.path = path
        super().__init__(_message_text(message))


def _message_text(message: str | Callable[[], str]) -> str:
    """把 ``str`` / i18n callable 统一成字符串（callable 失败不炸）。"""
    if callable(message):
        try:
            return str(message())
        except Exception:  # noqa: BLE001  翻译失败不能盖掉真正的校验错误
            return "validation failed"
    return str(message)


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


class ConfigDeclarationConflict(ConfigError):
    """同一个 ``(section, key)`` 被多处 ``@configclass`` 声明，且**声明不一致**。

    同名共享是允许的（多个类读同一个配置项），但前提是声明完全等价：类型一致、
    默认值一致。不一致就说明「同一个格子有两种解释」，此时无论采用哪一份都会
    让另一份的作者拿到意料之外的值，所以**装饰期当场报错**，而不是让后写者
    静默覆盖前者。

    继承 ``ConfigError`` 而不是 ``RuntimeError``：这条错误只会出现在导入 /
    装配期（``import`` 时炸），不会发生在运行期读取路径上，上层不需要为它
    准备"降级到默认值"的分支。
    """


class ConfigClassNotRegistered(RuntimeError, ConfigError):
    """``@configclass`` 类不在登记表里（从未声明，或 ``_CLASSES`` 被清空）。

    为什么继承两个基类：

    * ``RuntimeError`` —— 这是它的**历史类型**。``registry._ConfigSlot`` 过去
      直接抛裸 ``RuntimeError``，既有 ``except RuntimeError`` 与测试都不用改；
    * ``ConfigError`` —— 让上层"配置系统不可用 → 退回默认值"的分支只需要
      ``except ConfigError`` 一棵树，不必再兜一次宽 ``RuntimeError``
      （那会把调用方真正的编程错误也吞掉）。

    典型触发：某处借用了别的类的描述符（``class Stranger: x = Core.x``）、
    测试里清空了 ``_CLASSES``，随后仍然 ``Config()`` 读字段。

    注意：**同名 section 的多个 ``@configclass`` 不再互相顶出登记表**——声明
    一致时它们共存并共享取值（见 ``ConfigDeclarationConflict``）。
    """
