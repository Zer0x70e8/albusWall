#
"""会话脏标：用于崩溃恢复。

「干净」是唯一的肯定判断——文件存在且首行是 "clean"。
其余一切情况（不存在 / 空 / 乱码 / 写着 dirty / 权限错）
一律视为脏。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from albuswall.log import getLogger, Logger

_logger = getLogger(__name__)

_STATE_RUNNING = "dirty"
_STATE_CLEAN = "clean"


class SessionMarker:
    def __init__(
            self,
            path: Path,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._path = Path(path)
        self._log = log or _logger

    def is_clean_shutdown(self) -> bool:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
        except FileNotFoundError:
            self._log.debug("marker missing (%s) -> dirty", self._path)
            return False
        except OSError as exc:
            self._log.warning("marker unreadable (%s): %s", self._path, exc)
            return False
        return first == _STATE_CLEAN

    def mark_running(self) -> None:
        self._write(_STATE_RUNNING)

    def mark_clean(self) -> None:
        self._write(_STATE_CLEAN)

    def _write(self, state: str) -> None:
        content = f"{state}\n{_now_iso()}\n{os.getpid()}\n"
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            self._log.warning("failed to write marker %s: %s", self._path, exc)
            try:
                tmp.unlink()
            except OSError:
                pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
