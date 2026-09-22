#
""""""

from enum import Enum
from typing import Optional, Any

class ConfigueErrorSource(str, Enum):
    """配置加载来源"""
    ENV = "env"
    ARG = "arg"
    FILE = "file"

class ConfigError(Exception):
    """统一的配置错误，包含来源、键名、原始值、根因。"""
    def __init__(
        self,
        message: str,
        *,
        source: ConfigueErrorSource,
        key: str,
        value: Optional[Any] = None,
        cause: Optional[Exception] = None,
    ):
        self.source = source
        self.key = key
        self.value = value
        self.cause = cause
        # 构造错误描述，source 可直接当字符串
        desc = f"[{source}] key={key!r} value={value!r}: {message}"
        if cause is not None:
            desc += f" (caused by: {cause!r})"
        super().__init__(desc)

    # 可选类方法，方便按来源快速构造
    @classmethod
    def from_env(cls, message, key, value=None, cause=None):
        return cls(message, source=ConfigueErrorSource.ENV, key=key, value=value, cause=cause)

    @classmethod
    def from_arg(cls, message, key, value=None, cause=None):
        return cls(message, source=ConfigueErrorSource.ARG, key=key, value=value, cause=cause)

    @classmethod
    def from_file(cls, message, key, value=None, cause=None):
        return cls(message, source=ConfigueErrorSource.FILE, key=key, value=value, cause=cause)
