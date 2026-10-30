#
"""IPC 适配器：把 IPC 线路上的请求转交给 CommandService。

只做三件事：
1. 生命周期：start/shutdown 挂摘 IpcManager handler
2. 协议适配：IPC req(dict) -> CommandService.execute_request
3. 异常翻译：DispatchError -> IpcRemoteError（含 code 透传）

不做：查表、权限、参数绑定 —— 那是 CommandService/Dispatcher 的事。
"""

from __future__ import annotations

import threading
from typing import Any

from albuswall.log import getLogger
from albuswall.infrastructure.ipc import IpcManager, IpcRemoteError

from .dispatcher import DispatchError
from .service import CommandService

logger = getLogger(__name__)


class IpcRequestService:
    """持久化对象，容器单例友好。"""

    def __init__(self, manager: IpcManager, service: CommandService, *, log=None):
        self._manager = manager
        self._service = service
        self._log = log or logger
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._manager.set_handler(self)
            self._started = True
            self._log.info("IPC request service started.")

    def shutdown(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._manager.clear_handler()
            self._started = False
            self._log.info("IPC request service stopped.")

    @property
    def started(self) -> bool:
        return self._started

    def __call__(self, req: dict) -> Any:
        self._log.debug("IPC request: %r", req)
        try:
            return self._service.execute_request(req)
        except DispatchError as e:
            # code 已由 AppError.__init_subclass__ 保证非空；exit_code 由异常类
            # 自己声明（``DispatchError.exit_code`` 是 ``ExitCode``），这里只做
            # 「转交」——IPC 层不再沿异常因果链回捞，见 _service.py。
            raise IpcRemoteError(
                str(e),
                code=e.code,
                exit_code=getattr(e, "exit_code", None),
            ) from e
        except IpcRemoteError:
            raise
        except Exception as e:
            self._log.exception("command service crashed")
            raise IpcRemoteError(
                f"{type(e).__name__}: {e}", code="internal_error",
            ) from e
