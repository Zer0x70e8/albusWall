#
"""内置插件：进程间调用端点（IPC server）。

做成插件而不是无条件注册，是为了让两件事可分开控制：

* ``config.ini`` 的 ``[plugin_disables] ipc = ...`` 可以整体关掉它
  （不注册容器项、不挂生命周期钩子，连配置声明都不会生效）；
* ``[ipc] enabled = false`` 只关监听，命令层仍然就位。

下面 ``@declare`` 的函数**先** import 配置声明模块，让 ``ConfigField``
的默认值在静态配置加载之前完成登记——顺序错了默认值就注入不进去。
"""

from albuswall.plugin.declaration import declare

PLUGIN_ID = "ipc"


@declare(PLUGIN_ID)
def setup(container) -> None:
    # noinspection unused-imports
    from albuswall.infrastructure.ipc import config  # noqa: F401  静态配置声明
    from albuswall.infrastructure.ipc.bootstrap import register_ipc

    register_ipc(container)
