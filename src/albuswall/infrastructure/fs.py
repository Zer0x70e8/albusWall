#
"""文件系统原语。

与领域无关：不认识的「缩略图」「图片」等概念，只处理路径与字节。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from albuswall.log import getLogger

_logger = getLogger(__name__)


def atomic_write(dest: str | Path, writer: Callable[[Path], None]) -> None:
    """原子写入：writer 写临时文件，成功后 os.replace 到 dest。

    契约：
      - 自动创建 dest 的父目录
      - 临时文件与 dest 同目录，保证 os.replace 不跨文件系统
      - writer 抛异常、或 os.replace 失败时，尽力清理临时文件
      - 成功返回后 dest 一定是完整内容，绝不会出现半截文件

    Args:
        dest: 目标路径
        writer: 接收临时路径、负责写入内容的回调。
                抛出的任何异常都会传播给调用方。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 同目录 + pid + tid 后缀，避免并发写同一 dest 时互相覆盖临时文件
    tmp = dest.with_name(
        f".{dest.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )

    committed = False
    try:
        writer(tmp)
        os.replace(tmp, dest)
        committed = True
    finally:
        if not committed and tmp.exists():
            try:
                tmp.unlink()
            except OSError as exc:
                _logger.warning("failed to remove temp file %s: %s", tmp, exc)


def atomic_write_bytes(dest: str | Path, data: bytes) -> None:
    """``atomic_write`` 的 bytes 便利封装。"""

    def _write(p: Path) -> None:
        with open(p, "wb") as f:
            f.write(data)

    atomic_write(dest, _write)
