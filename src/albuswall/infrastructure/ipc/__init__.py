#
"""IPC：让运行中的 AlbusWall 能被别的进程（CLI / 脚本）调用。

* :mod:`albuswall.utils.ipc_endpoint` —— 地址与 token 的唯一解析处（两端共用）
* :mod:`albuswall.common.ipc`        —— 帧格式 / 错误码 / 两端共用的异常
* :mod:`albuswall.services.command`  —— ``@command`` 注册表 + 跨线程 Dispatcher
* :mod:`albuswall.infrastructure.ipc.server` —— 监听、连接、转发
* :mod:`albuswall.client`            —— 客户端（不 import 本包，零 GUI 依赖）
"""

from .bootstrap import register_ipc

__all__ = ["register_ipc"]
