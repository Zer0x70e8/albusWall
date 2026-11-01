#
"""导入域命令：``import.*``（手动推进导入 worker）。

为什么需要这组命令
------------------
正常链路是**自动**的：``SourceRuntime.scan_finished`` → 装配层把它接到
``ImportService.trigger()`` → worker 醒来把 ``asset_candidate_cache`` 里的
``pending`` 行落库。也就是说「建源 → 扫描 → 导入」不需要任何人按按钮。

但自动链路有两个盲点，都得靠手动触发兜底：

* 进程异常退出时可能留下 ``pending`` 行，而那一轮扫描的 ``scan_finished``
  永远不会再来；
* 调试 / 运维希望「立刻跑一轮」而不想等下一次扫描。

``trigger()`` 只是把 worker 的唤醒事件置位（``threading.Event.set``），
不写库、不碰主线程状态，因此从 IPC 线程直接调用是安全的 —— 真正的活由
worker 线程自己按 ``_run_lock`` 串行执行。

线程语义
--------
本模块**不做**任何线程处理（与 ``handlers/source.py`` 一致）：``trigger``
不阻塞、不等结果，只表示「已唤醒」。想看结果请查 ``source.tasks``
（``last_scan.inserted_count``）或日志。
"""

from __future__ import annotations

from albuswall.command import CommandRegistry
from albuswall.core import Container

from ._namespace import register_namespace

__all__: list[str] = []


def setup(registry: CommandRegistry, container: Container) -> None:
    # ``import_service`` 由 ``service/__init__.py`` 装配（当前仍住在
    # ``service/old/import_.py``，接口面不变）。
    import_service = container.require("import_service")

    @registry.command(
        "import.trigger",
        help_="立刻唤醒导入 worker 跑一轮（幂等；不等待结果）",
        params=(),
    )
    def import_trigger() -> dict:
        import_service.trigger()
        return {"triggered": True}

    # ---- 命名空间入口（``albuswall import trigger``）----
    register_namespace(
        registry, "import",
        help_="导入命令入口：手动推进导入 worker（import <sub> ...）",
        hint="正常链路是自动的（扫描完成 → worker）；这个只用于干预。",
    )
