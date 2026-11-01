#
"""命名空间入口：``<域> <subcommand> [...]`` → 转发到 ``<域>.<subcommand>``。

为什么需要这一层
----------------
命令注册用的是一段式的点分路径（``asset.list`` / ``tag.alias.create``），
而人（和 shell）习惯写成空格分隔的两段：

.. code-block:: text

    albuswall asset list 00000000-...        # 等价于分派 asset.list
    albuswall album create 旅行 --description=...
    albuswall tag alias list

``task`` 命令从一开始就是这个形状（裸 ``task`` 打用法，``task stats`` 干活），
本模块把同一套形状抽出来给其余域复用，**语义完全一致**：

* 裸 ``<域>``           → 打印子命令清单（不报错）；
* ``<域> <sub> ...``    → 用 :class:`Dispatcher` 分派到 ``<域>.<sub>``
  （参数绑定 / 异常翻译 / 权限判定全部走同一条路，不另写一套）；
* 未知子命令            → 抛 :class:`UnknownCommand`（exit 8），与打字打错
  一个已有命令的处理一致。

为什么用 ``Dispatcher`` 二次分派而不是手写 ``match subcmd``
----------------------------------------------------------
手写分支会漏掉参数绑定、``BadRequest`` 收口、服务层异常翻译器这些既有逻辑，
且每加一条命令要改两处。二次分派只有一条规则："把剩下的 token 原样交给
子命令"，和行为与直接调 ``asset.list`` 完全一致。

代价是每次调用多构造一个 ``Dispatcher``（纯函数式对象，无 IO）；这对命令
频率来说可以忽略，换来的是"命名空间与叶子命令永远同源"。
"""

from __future__ import annotations

from typing import Any, Callable

from albuswall.command import CommandRegistry, Param
from albuswall.command.exceptions import UnknownCommand

__all__ = ["register_namespace"]


def register_namespace(
        registry: CommandRegistry,
        path: str,
        *,
        help_: str,
        hint: str = "",
) -> Callable[..., Any]:
    """给 ``path`` 注册一个"域入口"命令，转发到 ``path.<sub>``。

    :param registry: 已注册了 ``<path>.<sub>`` 叶子命令的注册表。**必须在叶子
        之后调用**：内部用法清单与转发都依赖注册表里已存在的节点。
    :param path: 域名，如 ``"asset"``。
    :param help_: 一行说明（``help`` 输出用）。
    :param hint: 裸调用时追加在用法清单后面的一句话（可选）。
    """

    def _children(*, include_hidden: bool) -> list[tuple[str, str]]:
        """``[(子命令名, 说明), ...]``，按名字排序。"""
        prefix = f"{path}."
        result: list[tuple[str, str]] = []
        for name in registry.names(
                include_hidden=include_hidden, subject=frozenset({"admin"}),
        ):
            if not name.startswith(prefix):
                continue
            node = registry.get(name)
            if node is None or node.func is None:
                continue
            result.append((name[len(prefix):], node.help))
        return sorted(result)

    def _usage(*, include_hidden: bool) -> str:
        rows = _children(include_hidden=include_hidden)
        width = max((len(name) for name, _ in rows), default=0)
        lines = [
            f"用法: albuswall {path} <subcommand> [args...] [--key=value ...]",
            "",
            "子命令:",
        ]
        lines.extend(
            f"  {name:<{width}}  {help_text}" for name, help_text in rows
        )
        if not include_hidden:
            lines.append("")
            lines.append("（加 --all 查看隐藏命令，如维护 / 清理类）")
        if hint:
            lines.append("")
            lines.append(hint)
        return "\n".join(lines)

    @registry.command(
        path,
        help_=help_,
        # ``subcmd`` 只为 help 展示；真正的 token 走 ``pass_raw``（与 ``task``
        # 同一写法）。位置参数的可选值必须显式给 default，否则绑定层会填 None。
        params=(Param(name="subcmd", type=str, default=""),),
        pass_raw=True,
    )
    def namespace(args: list, kwargs: dict) -> Any:
        tokens = list(args)
        if not tokens:
            return _usage(include_hidden=bool(kwargs.get("all")))
        if kwargs.pop("help", False):
            return _usage(include_hidden=True)

        sub = str(tokens[0]).strip()
        if not sub:
            return _usage(include_hidden=bool(kwargs.get("all")))

        target = f"{path}.{sub}"
        node = registry.resolve(target)
        if node is None or node.func is None:
            raise UnknownCommand(target)

        # 懒导入：handlers 包与 dispatcher 之间没有模块级依赖，避免
        # import 顺序把这条边变成环。
        from ..dispatcher import Dispatcher

        return Dispatcher(registry).dispatch(target, tokens[1:], dict(kwargs))

    return namespace
