#
"""命令适配层装配入口。

显式列出每个 handler 模块；新增业务域时在这里加一行，不要用目录扫描。
builtin 排第一：诊断/自省能力先注册，后续业务命令注册后 help 即可见。

只读观测命令（album/asset/tag/thumb/trash/source）已随那批"最小接口"
service 一起退役——重新实现业务时按新契约重建 handler 模块，再加回
``MODULES``。目前业务域（与 ``service/`` 下的包一一对应）：

* ``source``    —— ``service/source/`` 五层 + 源回收站（``source.trash`` 等）；
* ``import``    —— 手动唤醒导入 worker（正常链路是扫描完成信号自动驱动）；
* ``asset``     —— 资产写命令（收藏 / 回收站 / 相册成员 / 标签绑定）；
* ``album``     —— 相簿生命周期（主语是"一个相簿"）；
* ``tag``       —— 标签树 + 别名（``tag.alias.*``）；
* ``trash``     —— **资产级**回收站（保留期清理 / 清空 / 恢复）；
* ``thumbnail`` —— 按需生成 + 维护（统计 / 路径 / 手动催 / 清孤儿）；
* ``keymap``    —— 快捷键层内省（``keymap.slots``）；注册表由 UI 启动流程
  后注册，命令懒取，取不到就提示；
* ``ui``        —— 界面配置内省（``ui.shortcuts`` / ``ui.state``），站在
  ``albuswall.ui.window.settings``（界面层的配置使用接口）上：输出快捷键
  配置与已保存的界面状态。注册表同样是懒取，没有界面也能读配置文件；
* ``setting``   —— 配置读写（结构内省 / 校验后写盘 / 脏节点落盘）。
  **例外**：它依赖容器的 ``config`` 键，因此不进
  ``tests/test_handlers_contract.py`` 的 ``_BUSINESS_HANDLERS`` 白名单；
  ``setup`` 里也不 ``require("config")``（测试用容器没有这个键），
  配置实例在命令函数体内懒取。
"""

from __future__ import annotations

from albuswall.command import CommandRegistry
from albuswall.core import Container

from . import (
    album,
    asset,
    builtin,
    import_,
    keymap,
    setting,
    source,
    tag,
    task,
    thumbnail,
    trash,
    ui,
)
from ._protocol import HandlerModule

MODULES: tuple[HandlerModule, ...] = (
    builtin, setting, task, source, import_, asset, album, tag, trash, thumbnail,
    # 只读内省（keymap / ui 的依赖由 UI 启动流程写入容器，命令体内懒取）
    keymap, ui,
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
