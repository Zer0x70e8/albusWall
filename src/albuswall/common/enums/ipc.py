#
"""ipc 域枚举：JSON-RPC 风格错误码。

数值沿用 JSON-RPC 2.0 的保留区间，方便未来接入标准客户端：

* ``-32768 ~ -32000``：协议层保留
* ``-32099 ~ -32000``：实现自定义区间（本项目用 ``-32001`` / ``-32002``）
"""

from enum import Enum


class IpcErrorCode(int, Enum):
    """跨进程调用的错误码。

    继承 ``int`` 保证直接落进 JSON 的数字字段，而不是 ``"IpcErrorCode.X"``。
    """

    PARSE_ERROR = -32700  # 帧不是合法 JSON
    INVALID_REQUEST = -32600  # 缺 method / 结构不对
    METHOD_NOT_FOUND = -32601  # 没有注册该命令
    INVALID_PARAMS = -32602  # params 与命令签名不匹配
    INTERNAL_ERROR = -32603  # 命令内部抛了非预期异常
    TIMEOUT = -32001  # 主循环在超时内没有回应（GUI 卡住）
    UNAVAILABLE = -32002  # 服务正在关闭，拒绝新请求
