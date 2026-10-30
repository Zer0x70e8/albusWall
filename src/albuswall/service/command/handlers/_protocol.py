#
"""Handler 模块契约：``handlers/<domain>.py`` 必须满足的规范接口。

``handlers/`` 是命令适配层——把外部入口（CLI / IPC / GUI）翻译成 service
调用。为了让 ``handlers/__init__.py`` 和 bootstrap 能统一装配，每个
handler 模块只暴露一个模块级 ``setup``，不导出任何其他名字。

本文件只定义结构，不 import 任何具体 handler 模块，避免循环导入。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from albuswall.command import CommandRegistry
from albuswall.core import Container


@runtime_checkable
class HandlerModule(Protocol):
    """业务命令适配模块的结构契约。

    ``setup`` 在 bootstrap 阶段被调用**恰好一次**，时机约定：

    - 所有业务 service 已注册到容器之后
    - ``registry.freeze()`` 之前

    因此 ``setup`` 里可以安全地 ``container.require(...)``；一旦 ``freeze``
    之后再注册会抛 ``RegistryFrozen``。

    编写 handler 的硬约束：

    1. **依赖在 ``setup`` 顶部解析**，闭包捕获，handler 函数体内不再调
       ``container.get`` / ``container.require``。解析一律用
       ``container.require``：缺依赖时它在启动期抛 ``MissingDependencyError``
       （``KeyError`` 子类），装配失败立即暴露；``container.get`` 会把这个
       异常吞成 ``None``，把失败推迟到用户敲命令时才炸。
    2. **依赖纪律：handler 只调 service，不调 ``*_repo``**。repo 是 service
       的内部依赖，是分层细节，不出现在 handler 里。需要新数据时先在
       ``service/`` 补一个**最小接口**（只暴露当前命令用到的方法，不要
       顺手设计完整 API），再让 handler 依赖容器 key；缺 service 不是
       「handler 直调 repo」的理由。同时**不 import 业务 service 的具体类** ——
       只依赖容器 key；需要类型安全时给 service 定义 ``Protocol``。

       新增 service 时，``service/__init__.py`` 的 ``Services.__annotations__``
       与 ``_ARG_MAP`` **必须同步添加**，缺一不可：``build_getters`` 的默认
       工厂是 ``type_(container.get("db"))`` —— 只写 ``Services`` 不写
       ``_ARG_MAP``，服务会被塞进一个 **db 连接器**当第一个位置参数（repo），
       构造居然能成功，然后在该服务第一次被调用时炸 ``AttributeError``；
       这比启动期 ``MissingDependencyError`` 难查得多。
    3. **handler 函数只做三件事**：取参数 → 调 service → 返回可序列化
       结果（dict / list / 基本类型 / str）。超过约 15 行说明逻辑该
       下沉到 service。
    4. **不抛非 ``DispatchError`` 的异常**。``Dispatcher`` 会兜住并包装
       成 ``CommandFailed``，但业务错误应抛 ``DispatchError`` 子类以
       获得正确的 ``code`` 和 ``exit_code``。
    5. **``params`` 的 schema 属于命令层**。``Param.name/kind/short/help``
       是给 CLI/GUI 看的契约，不要从 service 的签名反推。注意
       ``Param.type`` **不参与运行时转换**：``Dispatcher._bind`` 只校验
       ``choices``，值原样交给 handler。类型由入口保证——CLI 侧是
       ``ast.literal_eval``；GUI / IPC 直接构造请求的调用方需自行强转，
       需要强约束时在 handler 里显式校验（如 ``require_uuid``）。
       **``Param.name`` 必须是合法 Python 标识符**（下划线风格，如
       ``asset_uuid`` / ``include_deleted``）：``Dispatcher`` 用
       ``**bound_kwargs`` 调 handler，带连字符的名字（``asset-uuid``）
       会直接 ``TypeError`` → ``CommandFailed``。面向用户的连字符写法
       只出现在 ``help`` / CLI 展示层，不进 ``Param.name``。
    6. **``groups`` 与 ``acl`` 语义不同**。``acl`` 非空时**完全替代**
       ``groups`` 做执行判定；``groups`` 只影响可见性（``help`` 列表、
       菜单、补全）。所以 ``groups={"admin"}, acl={"readonly"}`` 的语义是
       「admin 看得见，但只有 readonly 身份能执行」。``subject`` 为空
       ``frozenset()`` 表示**匿名**（不是"跳过权限"）：任何非空的
       ``groups`` / ``acl`` 都不放行。
    7. **``pass_raw`` 不允许用于业务命令**。它是诊断 / 自省类命令的特权
       （现唯一用例是 ``help``），把原始 ``(args, kwargs)`` 直接交给
       handler，绕过 ``Param`` 绑定。业务命令一律用 ``Param`` schema——
       否则 GUI 生成不出表单、CLI 提示不出用法、review 时直接拒绝。
       代码层没有运行时拦截，靠 review 拦。
    8. **handler 跑在 IPC 工作线程**（线程名 ``ipc-conn``），不是主循环
       线程。因此 handler 只能调 service，**不得**触碰主线程属主的状态
       （Qt 控件、主循环事件队列等）；需要回到主线程由 UI 层自行投递。
       同理不要在 handler 里做长时间阻塞操作——它占住的是那一条连接的
       工作线程。

    满足此契约的模块在 ``handlers/__init__.py`` 里显式列出即可，
    **不要**用 ``importlib`` 扫描目录自动发现——注册顺序和失败点都
    会变得不可预测。

    示例::

        # handlers/album.py
        from albuswall.command.model import Command, Param
        from albuswall.command.registry import CommandRegistry
        from albuswall.core import Container


        def setup(registry: CommandRegistry, container: Container) -> None:
            # 示例假设该 key 已注册；容器 key 命名自由，这里只是占位名。
            svc = container.require("<your_service_key>")

            def album_list(limit: int = 20) -> list[dict]:
                return svc.list(limit=limit)

            def album_remove(name: str, *, force: bool = False) -> dict:
                return svc.remove(name, force=force)

            registry.register(Command(
                path="album.list",
                func=album_list,
                help_="列出相册",
                groups=frozenset({"user"}),
                params=(
                    Param("limit", kind="opt", type=int,
                          default=20, help="返回数量"),
                ),
            ))
            registry.register(Command(
                path="album.remove",
                func=album_remove,
                help_="删除相册",
                groups=frozenset({"user"}),
                acl=frozenset({"admin"}),
                params=(
                    Param("name", kind="pos", required=True,
                          help="相册名"),
                    Param("force", kind="flag", type=bool,
                          default=False, help="跳过确认"),
                ),
            ))

            # 反例：container.get(...) 缺依赖返回 None，直到命令被调用才
            #       AttributeError
            # 正例：container.require(...) 缺依赖启动期 MissingDependencyError
            #       （KeyError 子类）
    """

    def setup(
            self,
            registry: CommandRegistry,
            container: Container,
    ) -> None:
        """注册本域的所有命令。

        :param registry: 进程默认注册表。此时尚未 ``freeze``。
        :param container: 已装配好业务 service 的容器。
        """
        ...
