#
"""pytest 共享 fixture。

三条硬约束：

* ``albuswall.command.CommandRegistry`` 的默认实例是**进程全局单例**
  （``get_default_registry()``），一旦 ``freeze()`` 就无法复用 —— 任何
  用例都不得 import 它来装配命令，一律用 ``fresh_registry`` 新建。
* handler 的依赖边界是 **service**（不是 repo）：stub 层放在 service 上，
  这样用例同时验证「handler 只跟 service 说话」这条契约。repo 是 service
  的内部实现细节，测试不感知。
* 绝不连真 DB、绝不碰 ``~/.local/share``。

只读观测命令（album/asset/tag/thumb/trash/source）已退役，对应的
service stub 一并移除；留着 ``FakeTaskService`` 是因为 ``task.stats``
仍是唯一依赖业务 service 的内置命令。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from albuswall.command import CommandRegistry
from albuswall.core import Container
from albuswall.dto.thumbnail.paths import ThumbnailPaths
from albuswall.dto.thumbnail.query import MissingThumbnailRow
from albuswall.service.command.handlers import setup as setup_handlers


# ==================== 构造辅助 ====================

def make_missing_row(
        *,
        asset_id: int = 5,
        uuid: UUID | None = None,
        source_id: int | None = None,
        base: str | None = "/thumbs",
        small: str | None = "s.jpg",
) -> MissingThumbnailRow:
    """构造一个 ``MissingThumbnailRow``（含嵌套 ``ThumbnailPaths``）。"""
    return MissingThumbnailRow(
        id=asset_id,
        uuid=uuid or uuid4(),
        source_id=source_id,
        file_path="2024/a.jpg",
        file_hash="deadbeef",
        paths=ThumbnailPaths(base=base, small=small),
    )


# ==================== stub：service 层 ====================

class FakeTaskService:
    """``task_service`` 的最小 stub（infra 层服务，本身就是 service）。

    ``TaskServiceStats`` 在 ``__init__`` 里**局部 import**，不放在模块顶部：
    ``albuswall.dto.task`` 顶部会触发 ``albuswall.infrastructure.__init__``，
    后者又反向 import ``infrastructure.task.service`` → ``dto.task``，这是
    一个**既有**的循环 import（``dto/`` 与 ``infrastructure/`` 都不在本批
    白名单）。「谁先被 import」决定成败，所以延到调用期，保证 handlers
    （间接拉起 infrastructure）已经先加载完。
    """

    def __init__(
            self,
            *,
            submitted: int = 42,
            rejected: int = 2,
            backpressure_reject: int = 3,
            backpressure_wait: int = 1,
            last_backpressure_ts: float = 1700000000.5,
            queue_size: int = 7,
            global_concurrency_max: int = 8,
            global_concurrency_available: int = 5,
            global_concurrency_current: int = 3,
    ) -> None:
        from albuswall.dto.task import TaskServiceStats

        self._stats = TaskServiceStats(
            submitted=submitted,
            rejected=rejected,
            backpressure_reject=backpressure_reject,
            backpressure_wait=backpressure_wait,
            last_backpressure_ts=last_backpressure_ts,
        )
        self._queue_size = queue_size
        self._max = global_concurrency_max
        self._available = global_concurrency_available
        self._current = global_concurrency_current
        self.calls: list[str] = []

    def get_stats(self):
        self.calls.append("get_stats")
        return self._stats

    @property
    def queue_size(self) -> int:
        return self._queue_size

    @property
    def global_concurrency_max(self) -> int:
        return self._max

    @property
    def global_concurrency_available(self) -> int:
        return self._available

    @property
    def global_concurrency_current(self) -> int:
        return self._current


# ==================== fixture ====================

@pytest.fixture
def fresh_registry() -> CommandRegistry:
    """每个用例一张全新注册表，绕开进程全局单例与 freeze。"""
    return CommandRegistry()


@pytest.fixture
def fake_container() -> Container:
    """只装了 **service** stub 的容器（不含任何 repo）。

    handler 的 ``setup`` 用 ``container.require("xxx_service")`` 取依赖；
    容器里没有 ``view_repo`` 之类的 key —— 若某个 handler 误改为直调 repo，
    这里会直接 ``MissingDependencyError``，比断言更硬。
    """
    container = Container()
    container.set("task_service", FakeTaskService())
    return container


@pytest.fixture
def assembled(fresh_registry, fake_container):
    """装配好全部 handler 的 (registry, container) —— 只注册，不 freeze。"""
    setup_handlers(fresh_registry, fake_container)
    return fresh_registry, fake_container


@pytest.fixture
def clean_hooks():
    """快照并恢复全局 hook 表：``register_service`` 会往里追加 on_loop。

    全局 ``hooks_map`` 是进程级的，用例之间不清会互相污染。
    """
    from albuswall.core.registry import hooks_map

    saved = {name: list(hooks) for name, hooks in hooks_map.items()}
    try:
        yield hooks_map
    finally:
        for name, hooks in saved.items():
            hooks_map[name][:] = hooks


# ==================== 全局状态复位 ====================

@pytest.fixture(autouse=True)
def _reset_global_plugin_state():
    """每个用例结束后复位进程级全局状态。

    插件系统把三处状态放在模块 / 类级（而不是实例上），它们天然跨用例泄漏：

    * ``core.registry.hooks_map``——插件 ``ctx.on(...)`` 会往里追加 hook；
    * ``core.plugins._pending``——``@plugin`` 的收集表，留着会让下个用例
      意外多出插件；
    * ``Application`` / ``CoreApplication`` / ``Configue`` 的单例字段。

    hook 用**原地切片赋值**恢复：``hooks_map[event]`` 这个 list 对象可能已经
    被别的模块持有引用（例如某个 hook 追加时拿到的就是它），换对象会让那些
    引用指向旧列表。
    """
    from albuswall.configue.configue import Configue
    from albuswall.core.application import Application, CoreApplication
    from albuswall.core.plugins import _pending
    from albuswall.core.registry import hooks_map

    hook_snapshot = {k: list(v) for k, v in hooks_map.items()}
    pending_snapshot = list(_pending)
    inst, self_, init = Application._instance, Application._self, Application._is_init
    reg = CoreApplication._hooks_registered
    cfg = Configue._instance

    yield

    for k, v in hook_snapshot.items():
        hooks_map[k][:] = v        # 原地恢复，不换 list 对象
    _pending[:] = pending_snapshot
    Application._instance, Application._self, Application._is_init = inst, self_, init
    CoreApplication._hooks_registered = reg
    Configue._instance = cfg
