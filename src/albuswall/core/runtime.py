#
"""顶层异常 → 退出码映射；SIGINT 优雅退出。"""

import logging
import signal
from traceback import format_exception, format_exc
from typing import Callable

import albuswall
from albuswall.core import Application
from albuswall.log.handlers import MemoryCacheHandler

_logger = logging.getLogger(albuswall.__name__)


class Runtime:
    def __init__(self, memory_handler: MemoryCacheHandler) -> None:
        self._old_sigint = None
        self._memory_handler = memory_handler
        self._app = Application.instance()

    # ---------------- 退出码映射 ----------------
    @staticmethod
    def _control_flow_code(et, ev) -> int | str | None:
        if issubclass(et, KeyboardInterrupt):
            return 130
        if issubclass(et, SystemExit):
            code = getattr(ev, "code", 0)
            return 0 if code in (0, "0", None) else code
        return None

    # ---------------- 安装 ----------------
    def install(self) -> None:
        self._app.handle_exception = self._on_exception
        self._app.thread_exception = self._on_thread_exception
        self._old_sigint = signal.signal(signal.SIGINT, self._on_sigint)

    def _on_sigint(self, signum, frame) -> None:
        _logger.info("SIGINT received, requesting graceful exit")
        self._request_quit(130)

    def _on_exception(self, et, ev, tb) -> bool:
        code = self._control_flow_code(et, ev)
        if code is not None:
            _logger.info(
                "Control-flow exception %s received, exiting with code=%r",
                et.__name__, code,
            )
            self._request_quit(code)
            return True

        _logger.critical("".join(format_exception(et, ev, tb)))
        self._request_quit(1)
        return True

    def _on_thread_exception(self, args) -> bool:
        return self._on_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        )

    # ---------------- 退出请求 ----------------
    def _request_quit(self, code: int | str) -> None:
        # 用 _main_loop 实例属性；不要触发惰性创建
        loop = getattr(self._app, "_main_loop", None)
        if loop is None:
            _logger.debug("request_quit(%r): no main loop yet", code)
            return
        try:
            loop.code = code
        except Exception:
            pass
        try:
            loop.quit()
        except Exception:
            _logger.exception("Failed to quit main loop")

    # ---------------- handlers 收尾 ----------------
    def _close_handlers(self) -> None:
        try:
            _logger.removeHandler(self._memory_handler)
        except Exception:
            pass
        try:
            self._memory_handler.close()
        except Exception:
            _logger.exception("Error while closing memory_handler")

    # ---------------- 顶层入口 ----------------
    def run(self, body: Callable[[], int | str]) -> int | str:
        try:
            return body()
        except SystemExit as e:
            if e.code not in (0, "0", None):
                self._close_handlers()
                _logger.error("Program exited with error code %s", e.code)
                _logger.error(format_exc())
                return e.code
            return 0
        except NotImplementedError:
            self._close_handlers()
            _logger.error(format_exc())
            _logger.warning("Not supported by the current platform.")
            return 95
        except KeyboardInterrupt:
            self._close_handlers()
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                Application.request_quit(reason="KeyboardInterrupt")
            except Exception:
                _logger.exception("Error while finalizing after KeyboardInterrupt")
            finally:
                signal.signal(signal.SIGINT, old)
            _logger.debug("KeyboardInterrupt received, exiting.")
            return 130
        except BaseException:
            self._close_handlers()
            _logger.critical(format_exc())
            return 1
