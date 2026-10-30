#
"""任务域命令：只读观测（``task.*``）。

依赖走 ``container.require``，在 ``setup`` 顶部解析一次并由闭包捕获；
handler 体内不再碰容器。``groups`` / ``acl`` 留空 —— 批次 1 的命令全匿名，
权限接线（subject provider）尚未落地。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from albuswall.command import CommandRegistry, Param
from albuswall.core import Container


def setup(registry: CommandRegistry, container: Container) -> None:
    task_service = container.require("task_service")

    @registry.command(
        "task",
        help_="The task service cmd.",
        params=(
                Param(name="subcmd", type=str),
        ),
    )
    def namespace(subcmd: str = ""):
        subcmd = subcmd.strip()
        if not subcmd:
            return "使用 `albuswall help task` 查看子命令。"
        match subcmd:
            case "stats":
                return task_stats()
        return f"Unknown subcommand: {subcmd}"

    @registry.command(
        "task.stats",
        help_="任务服务统计：队列、并发水位与背压计数",
        hidden=True,
        params=(),
    )
    def task_stats() -> dict[str, Any]:
        return {
            "stats": asdict(task_service.get_stats()),
            "queue_size": task_service.queue_size,
            "global_concurrency_max": task_service.global_concurrency_max,
            "global_concurrency_available":
                task_service.global_concurrency_available,
            "global_concurrency_current": task_service.global_concurrency_current,
        }
