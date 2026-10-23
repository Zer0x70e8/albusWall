#
"""IPC 的静态配置声明。

**本模块必须在静态配置加载之前被 import**：``ConfigField`` 的默认值是在
``__set_name__`` 时登记进 declaration 的，晚了就赶不上默认值注入。所以
``plugins/ipc.py`` 的 ``@declare`` 函数第一件事就是 import 本模块。

配置（``config.ini``）::

    [ipc]
    enabled = true
    call_timeout = 30
    max_message_bytes = 8388608
    max_connections = 8
    event_bridge = true
"""

from albuswall.configue import ConfigField


# noinspection bad-assignment
class Config:
    # 声明 section（不注入默认值），让 [ipc] 出现在生成的配置示例里
    root = ConfigField("ipc", default={})

    enabled: bool = ConfigField("ipc", "enabled", default=True)
    #: 单次调用从「排队」到「拿到结果」的总超时（秒）
    call_timeout: float = ConfigField("ipc", "call_timeout", default=30.0)
    #: 单帧上限；同时约束敌意客户端和超大 DTO
    max_message_bytes: int = ConfigField(
        "ipc", "max_message_bytes", default=8 * 1024 * 1024
    )
    #: 并发连接上限（CLI 是低频调用，给 8 足够）
    max_connections: int = ConfigField("ipc", "max_connections", default=8)
    #: 是否把内部 Signal 桥接成 IPC 事件推送
    event_bridge: bool = ConfigField("ipc", "event_bridge", default=True)
