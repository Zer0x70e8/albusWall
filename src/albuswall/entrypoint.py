# -*- coding: utf-8 -*-
"""入口决策树。"""

from __future__ import annotations

import sys

from albuswall.log import getLogger

_logger = getLogger(__name__)

_SUBCOMMANDS = frozenset({
    "status", "start", "stop", "restart", "reload", "version",
})
_CLIENT_FLAGS = frozenset({"-h", "--help_", "--cli"})


def should_launch_main() -> bool:
    """决定当前进程走主程序还是 CLI 客户端。

    Returns
    -------
    bool
        True  → 启动 main()，本进程作为主实例。
        False → 作为 CLI 客户端（子命令 / status）。

    Raises
    ------
    SystemExit
        已有实例且当前非交互、无桌面：提示/日志后退出。
    """
    if _is_client_request(sys.argv[1:]):
        return False

    from albuswall.infrastructure.ipc import (
        classify_instance, InstanceKind,
    )

    kind = classify_instance()
    if kind is InstanceKind.NONE:
        # 交给 main 内部 bind 端点做第二道竞争保护
        return True

    # ALIVE / STALLED 都视为已有实例
    if kind is InstanceKind.STALLED:
        _logger.warning("IPC 端点在手但业务无响应，保守判定为已有实例。")

    from albuswall.infrastructure.platform import (
        has_desktop, is_interactive, notify_already_running,
    )

    if is_interactive():
        return False

    if has_desktop():
        notify_already_running()
    else:
        _logger.warning("已有实例在运行，且当前无交互环境，静默退出。")
    raise SystemExit(0)


def _is_client_request(argv: list[str]) -> bool:
    return any(tok in _SUBCOMMANDS or tok in _CLIENT_FLAGS for tok in argv)
