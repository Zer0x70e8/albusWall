#
""""""

from enum import IntEnum

__all__ = ["ExitCode", "AppError"]


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    CONFIG_ERROR = 2
    USAGE_ERROR = 64
    SOFTWARE_ERROR = 70
    OS_ERROR = 71
    NOT_IMPLEMENTED = 95
    INTERRUPTED = 130

    # noinspection unresolved-references, bad-return
    @classmethod
    def _missing_(cls, value: object) -> "ExitCode | None":
        if isinstance(value, int) and not isinstance(value, bool):
            pseudo = int.__new__(cls, value)
            pseudo._name_ = f"CODE_{value}"
            pseudo._value_ = value
            return pseudo
        return None

    # noinspection protected-member
    @classmethod
    def from_code(cls, code: str | None) -> "ExitCode":
        """按协议错误码查退出码（客户端收到 IpcRemoteError 时用）。

        未知 / None -> ERROR，让"没归类"显式暴露，而不是伪装成 USAGE_ERROR。
        """
        err_cls = AppError._code_map.get(code or "")
        return err_cls.exit_code if err_cls is not None else cls.ERROR


class AppError(Exception):
    """携带进程退出码与协议错误码的应用异常基类。

    - ``code``:      协议字符串，IPC 序列化只发这个
    - ``exit_code``: 本进程 sys.exit 用的退出码

    子类**只声明**这两个类属性即可，``__init_subclass__`` 自动登记到
    ``_code_map``，``ExitCode.from_code`` 因此无需额外映射表。
    同 code 冲突在类定义时就 raise，避免运行期才发现。
    """

    code: str = ""
    exit_code: ExitCode = ExitCode.ERROR
    _code_map: dict[str, type["AppError"]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # 只认子类自己 dict 里的 code；继承来的 code 不重复注册，
        # 这样"忘了声明"不会意外覆盖父类的映射。
        code = cls.__dict__.get("code")
        if not code:
            return
        # noinspection bad-argument-type
        existing = AppError._code_map.get(code)
        if existing is not None and existing is not cls:
            raise RuntimeError(
                f"AppError code {code!r} already registered by "
                f"{existing.__qualname__}; conflict with {cls.__qualname__}"
            )
        # noinspection bad-index
        AppError._code_map[code] = cls
