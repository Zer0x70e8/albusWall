#
"""AlbusWall IPC 异常类型。"""
from __future__ import annotations


class IpcError(Exception):
    """基类。"""


class IpcProtocolError(IpcError):
    """帧格式错误 / 超长 / JSON 解析失败。"""


class IpcUnavailable(IpcError):
    """连不上端点：无实例 / 端点不存在 / 被拒 / 握手失败。"""


class IpcTimeout(IpcError):
    """连上了但等待响应超时。保守判定为「实例存活但卡住」。"""


class IpcRemoteError(IpcError):
    """对端返回 ok=False。

    - ``code``: 协议错误码，用于程序化判别
    - ``exit_code``: 对端给出的「建议退出码」；对端没给（纯 IPC 层错误、
      ``internal_error`` 等）时为 ``None``，由调用方用自己的默认值

    ``exit_code`` 是**构造参数**而不是事后挂属性：服务端在
    ``service/command/ipc_request_service.py`` 里把业务异常的
    ``exit_code`` 直接传下来，客户端在 ``_client.py`` 里同样走构造参数，
    两端签名对称。
    """

    def __init__(self, message: str, code: str = "remote_error",
                 exit_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class InstanceAlreadyRunning(IpcError):
    """bind 失败且探测到真实例存活。入口据此回落「已有实例」分支。"""

    def __init__(self, endpoint: str):
        super().__init__(f"endpoint already in use: {endpoint}")
        self.endpoint = endpoint
