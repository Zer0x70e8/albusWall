#
"""Top-level runtime: exception → exit-code mapping and logger lifecycle."""

from __future__ import annotations

import logging
import threading
import traceback
from traceback import format_exception
from typing import TYPE_CHECKING, Callable

import albuswall

from .exit_codes import ExitCode
from .application import Application, CoreApplication

if TYPE_CHECKING:
    from albuswall.log.handlers import MemoryCacheHandler
    from .main_loop import MainLoop

logger = logging.getLogger(albuswall.__name__)

__all__ = ["Runtime"]


class Runtime:
    """顶层运行时协调器。

    职责：
    * 顶层异常 → 规范退出码（统一为 ``ExitCode``）；
    * 安装 ``threading.excepthook``；
    * 管理 ``MemoryCacheHandler`` 的生命周期（移除 + 兜底重放）。

    不负责：
    * SIGINT/SIGTERM —— ``HeadlessMainLoop._install_signals``；
    * ``sys.excepthook`` —— ``albuswall.core.application`` 导入期的
      ``install_excepthook()``；
    * 主循环 —— ``Application._loop``。

    边界：``__main__.py`` 的 import 语句本身失败时，Runtime 还没被
    构造，异常只能走 Python 默认 ``sys.excepthook`` 到 stderr。
    """

    def __init__(self, memory_handler: "MemoryCacheHandler") -> None:
        self._memory_handler = memory_handler
        self._app: CoreApplication | None = None
        self._installed = False
        self._prev_thread_excepthook = None

    # ---------------- install ----------------
    def install(self) -> None:
        if self._installed:
            return
        self._app = Application.instance()
        self._prev_thread_excepthook = threading.excepthook
        threading.excepthook = self._thread_excepthook
        self._installed = True

    def _thread_excepthook(self, args: threading.ExceptHookArgs) -> None:
        thread = args.thread
        name = thread.name if thread is not None else "<unknown>"
        logger.critical("Unhandled exception in thread %r", name)
        exc_type = args.exc_type
        exc_value = args.exc_value
        exc_traceback = args.exc_traceback
        if exc_type is not None and exc_value is not None:
            for line in format_exception(exc_type, exc_value, exc_traceback):
                logger.critical(line.rstrip())
        self._request_quit(ExitCode.ERROR)

    # ---------------- quit dispatch ----------------
    def _request_quit(self, code: ExitCode | int) -> None:
        app = self._app
        if app is None:
            return
        loop: "MainLoop | None" = getattr(app, "_loop", None)
        if loop is None:
            logger.debug("request_quit(%r): no main loop yet", code)
            return
        try:
            loop.quit(code)
        except TypeError:
            # 兼容自定义 MainLoop 没升级签名
            try:
                loop.code = code
                loop.quit()
            except Exception:  # noqa: BLE001  # noinspection PyBroadException
                logger.exception("Failed to quit main loop")
        except Exception:  # noqa: BLE001  # noinspection PyBroadException
            logger.exception("Failed to quit main loop")

    # ---------------- handlers ----------------
    def _close_handlers(self) -> None:
        """移除并关闭启动期缓存 handler；幂等。"""
        try:
            logger.removeHandler(self._memory_handler)
        except Exception:  # noqa: BLE001  # noinspection PyBroadException
            pass

        buffer = getattr(self._memory_handler, "buffer", None)
        if isinstance(buffer, list) and buffer:
            try:
                fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
                fallback = logging.StreamHandler(getattr(
                    self._memory_handler, "stream", None
                ))
                fallback.setFormatter(logging.Formatter(fmt))
                for record in buffer:
                    fallback.handle(record)
                buffer.clear()
            except Exception:  # noqa: BLE001  # noinspection PyBroadException
                pass

        try:
            self._memory_handler.close()
        except Exception:  # noqa: BLE001  # noinspection PyBroadException
            logger.exception("Error while closing memory_handler")

    # ---------------- entry ----------------
    def run(self, body: Callable[[], ExitCode | int]) -> ExitCode:
        """运行 ``body`` 并把顶层异常统一映射为 ``ExitCode``。"""
        try:
            result = body()
            return result if isinstance(result, ExitCode) else ExitCode(result)
        except SystemExit as exc:
            code = self._system_exit_code(exc)
            self._close_handlers()
            if code != ExitCode.OK:
                logger.error("Program exited with error code %r", code)
            return code
        except KeyboardInterrupt:
            self._close_handlers()
            logger.info("KeyboardInterrupt received, exiting.")
            return ExitCode.INTERRUPTED
        except NotImplementedError:
            self._close_handlers()
            logger.error(traceback.format_exc())
            logger.warning("Not supported by the current platform.")
            return ExitCode.NOT_IMPLEMENTED
        except OSError:
            self._close_handlers()
            logger.exception("OS error")
            return ExitCode.OS_ERROR
        except BaseException:
            self._close_handlers()
            logger.critical(traceback.format_exc())
            return ExitCode.ERROR

    @staticmethod
    def _system_exit_code(exc: SystemExit) -> ExitCode:
        code = exc.code
        if code is None or code == 0 or code == "0":
            return ExitCode.OK
        if isinstance(code, int) and not isinstance(code, bool):
            return ExitCode(code)
        logger.error("SystemExit: %r", code)
        return ExitCode.ERROR

    def run_app(self, main: Callable[[Application], ExitCode | int]) -> ExitCode:
        def body() -> ExitCode | int:
            app = Application()
            self.install()
            return main(app)

        return self.run(body)
