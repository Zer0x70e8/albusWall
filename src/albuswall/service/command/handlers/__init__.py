#
"""命令适配层装配入口。

显式列出每个 handler 模块；新增业务域时在这里加一行，不要用目录扫描。
builtin 排第一：诊断/自省能力先注册，后续业务命令注册后 help 即可见。

只读观测命令（album/asset/tag/thumb/trash/source）已随那批"最小接口"
service 一起退役——它们的接口从未被承诺实现，重新实现业务时按新契约
重建 handler 模块，再加回 ``MODULES``。
"""

from __future__ import annotations

from albuswall.command import CommandRegistry
from albuswall.core import Container

from . import builtin, task
from ._protocol import HandlerModule

MODULES: tuple[HandlerModule, ...] = (
    builtin, task,
)


def setup(registry: CommandRegistry, container: Container) -> None:
    """按 ``MODULES`` 顺序注册所有命令。

    ``runtime_checkable`` 只能验证 ``setup`` 属性存在，挡得住拼写错误
    （如 ``def setupx``），挡不住签名反了（``setup(container, registry)``）。
    后者靠 mypy 或测试挡。
    """
    for module in MODULES:
        assert isinstance(module, HandlerModule), (
            f"{module.__name__} 未实现 HandlerModule 契约（缺 setup）"
        )
        module.setup(registry, container)
