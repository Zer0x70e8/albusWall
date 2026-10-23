#
"""客户端：从**其它进程**调用运行中的 AlbusWall。

设计约束（重要）
----------------
本包**不得** import ``albuswall.core`` / ``albuswall.services`` /
``albuswall.infrastructure``。原因：

* ``albuswall.core.application`` 在 import 期就构造 ``_Application()``、
  安装全局 ``sys.excepthook``，并可能 ``raise`` —— CLI 只想发一条消息，
  不该连带启动半个应用；
* ``albuswall.infrastructure`` 会拉起 apscheduler / psutil / SQLite 连接管理，
  对 ``albusctl ping`` 而言纯属浪费。

因此依赖闭包被刻意压到：``common.ipc``（协议）+ ``utils.ipc_endpoint``
（地址）+ ``multiprocessing.connection``（传输）+ 标准库。
"""

from .core import (
    AuthFailed,
    CallTimeout,
    IpcClient,
    IpcFailure,
    NotRunning,
    ProtocolError,
)

__all__ = [
    "AuthFailed",
    "CallTimeout",
    "IpcClient",
    "IpcFailure",
    "NotRunning",
    "ProtocolError",
]
