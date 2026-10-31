#
"""``configue`` —— 声明式配置系统。

和已删除的旧配置包相比，这一版把「声明」「加载」「访问」三层彻底
分开，去掉了全局 ``configure_persistence``，并且把文件格式降级成可插拔后端：

==================  ============================================================
层                  职责
==================  ============================================================
``registry``        数据结构 + 全局表 + ``reg()`` + ``@configclass`` + 描述符
``resolver``        值的唯一中枢（惰性 / 记忆化 / 类型转换 / origin 追踪）
``backends``        格式适配器（INI / JSON）+ 后端注册表（TOML / YAML 留位）
``serialization``   typed → 各后端的可写形态（``to_ini`` / ``to_json_native``）
``bootstrap``       ``FileInfo`` → 原始字典（只做分派，不认识格式）
``configue``        单例 + ``check()`` / ``load()`` / ``unload()`` / ``flush()``
``tree``            ``c.tree.<file>.<section>.<key>`` 三层视图
==================  ============================================================

典型用法::

    from albuswall.configue import (
        ConfigFile, Configue, configclass, field, reg,
    )

    reg(ConfigFile.INI, "config.ini", name="main", order=10)
    reg(ConfigFile.JSON, "state.json", name="state", order=20, autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False, label="调试")
        theme: str = "dark"

    c = Configue(base_dir=base_dir)   # 单例，base_dir 立即 resolve
    c.load()                          # 按 FileInfo.order 升序合并
    print(c.tree.main.core.debug)     # 树视图：无类型校验的值总线
    print(Core().debug)               # 类视图：严格 isinstance 赋值
    c.tree.main.save()                # == c.flush(file="main")

上面的 ``c.tree`` 两行**只是演示**。业务代码优先用 ``@configclass`` 类视图
（``Core().debug``）：声明即注册、默认值兜底、写入严格类型校验，落点由声明
决定而不是「当前视图是哪个文件」。树视图留给它的合法用途——``[shortcuts]``
这类运行期才知道 key 的段、以及「文件里实际有什么」的枚举。

三条硬约束：

1. ``Configue`` 是单例：同 ``base_dir`` 幂等，不同 ``base_dir`` 抛
   ``RuntimeError``。换根必须显式 ``Configue._instance = None``。
2. ``load()`` 幂等：``loaded=True`` 的文件跳过；加载失败不回滚已成功的文件。
3. 回写策略是**文件级**的（``FileInfo.autosave``），不再有全局开关；
   ``__set__`` 只负责标脏 + 按该文件策略调度。

本包刻意**不**提供模块级 ``tree``：``tree`` 是实例属性（``c.tree``），
模块级入口会诱导「不初始化就访问」。

扩展点（全部导入期安全）：

- ``register_converter(type_, reader, writer=)`` —— 「构造函数语义不对」的类型
- ``register_load_hook(hook)`` —— 运行期才知道 key 的段（``[shortcuts]`` 之类）
- ``register_backend(backend)`` —— 新文件格式（TOML / YAML 的落点）

两个**审计开关**（默认都关，零行为变化；打开后让「拼错的 key」显形）：

- ``load(strict_undeclared=True)`` —— 文件里有、但既无 ``@configclass``
  声明、也无加载钩子登记类型的节点，进 ``LoadReport.warnings``。
  只报告不隔离（强制隔离会误伤 ``[shortcuts]``）。
- ``Configue.strict_tree_writes = True`` —— ``tree.<file>.<section>.<key> = v``
  要求该 key 被声明或 hook 登记过，否则 ``AttributeError``。读侧不变。

**hook 异常不中断加载**：``load()`` 会把 hook 异常收集成
``LoadError(kind="hook_failed")``，``partial=False`` 时和其它错误一起统一抛
``ConfigError``；不会出现「一个 hook 炸了、后面文件全没加载」。
"""

from contextlib import suppress
from typing import Any, Callable

from .backends import (
    Backend,
    get_backend,
    register_backend,
    registered_backends,
)
from .configue import Configue
from .errors import (
    AmbiguousConfigFile,
    ConfigError,
    ConfigFileNotFound,
    ConfigMissingError,
    LoadError,
    LoadReport,
)
from .registry import (
    ROOT,
    AutosavePolicy,
    ConfigFile,
    FileInfo,
    classes_in,
    configclass,
    field,
    reg,
)

__all__ = [
    # 声明（模块级，导入期安全）
    "reg", "configclass", "field", "ROOT",
    "ConfigFile", "AutosavePolicy",
    "classes_in",
    # 主类
    "Configue",
    # 加载钩子（运行期才知道 key 的段用）
    "register_load_hook", "unregister_load_hook",
    # 自定义类型转换（读写成对注册）
    "register_converter",
    # 后端（扩展点：新格式 = 实现 Backend + register_backend）
    "Backend", "register_backend", "get_backend", "registered_backends",
    # 错误 / 报告
    "ConfigError", "ConfigMissingError",
    "ConfigFileNotFound", "AmbiguousConfigFile",
    "LoadReport", "LoadError",
    # 元数据（调试 / 工具脚本用，业务侧不建议直接依赖）
    "FileInfo",
]

#: ``Configue`` 构造**之前**登记的转换器，等 ``load()`` 时统一应用。
#:
#: 为什么放在模块级：转换器常常在插件导入期登记，那时还没有 ``Configue``
#: 实例（``base_dir`` 要等应用启动才知道）。放实例上就只能丢或者报错。
#:
#: 注意：``Configue.close()`` **不**清空它（close 之后仍可能重 ``load()``）；
#: 测试要在 fixture 里显式 clear，否则用例之间会互相污染。
_PENDING_CONVERTERS: list[
    tuple[Any, Callable[[str], Any], Callable[[Any], str] | None]
] = []

#: 加载钩子表：``hook(resolver) -> None``，见 :func:`register_load_hook`。
#:
#: 与 ``_PENDING_CONVERTERS`` 不同，它**不会被消费掉**——hook 是长期注册，
#: 每次 ``load()`` 都要跑（hook 自己负责幂等）。测试同样要显式 clear。
_LOAD_HOOKS: list[Callable[[Any], None]] = []


def register_load_hook(hook: Callable[[Any], None]) -> None:
    """登记一个「加载完成前」的钩子 ``hook(resolver) -> None``。

    用途：某些子系统的 key 是**运行期才知道的**（典型：``[shortcuts]`` 的
    ``<action_id>``），声明期写不出 ``@configclass`` 字段，只能等文件读进
    ``Resolver`` 之后按「段名 + 统一类型」批量补：

    .. code-block:: python

        from albuswall.configue import register_load_hook

        def _type_shortcuts(resolver):
            for key in resolver.keys_of("shortcuts"):
                resolver.register_type("shortcuts", key, list[str])

        register_load_hook(_type_shortcuts)

    调用时机：``Configue.load()`` 的 **Step 4（类型注册 + 默认值注入）之后、
    ``check_all`` 之前**。因此 hook 里 ``register_type`` 可以**覆盖**
    ``@configclass`` 声明的类型，并且覆盖后的类型会被 ``check_all`` 采用。

    幂等：同一个函数重复登记只入队一次。每次 ``load()`` 都会执行，hook 自己
    负责「重复执行也安全」（``register_type`` 本身幂等）。

    **失败语义**：hook 抛异常不会中断加载，会收集成
    ``LoadError(kind="hook_failed")`` 进 ``LoadReport.errors``；``partial=False``
    时和其它错误一起统一抛 ``ConfigError``。这与旧包（异常直接炸启动）不同。

    Args:
        hook: ``hook(resolver) -> None``。只应写内存（``register_type`` /
            ``keys_of`` 等），不要读盘或改 ``_FILES``。
    """
    if hook not in _LOAD_HOOKS:
        _LOAD_HOOKS.append(hook)


def unregister_load_hook(hook: Callable[[Any], None]) -> None:
    """取消登记（主要给测试 / 热插拔用）。未登记过时静默返回。"""
    with suppress(ValueError):
        _LOAD_HOOKS.remove(hook)


def register_converter(
        type_: Any,
        reader: Callable[[str], Any],
        *,
        writer: Callable[[Any], str] | None = None,
) -> None:
    """注册自定义类型转换器（模块级便捷入口）。

    等价于 ``Configue._instance.resolver.register_converter(...)``，但在
    ``Configue`` 尚未构造时**入队**，等 ``load()`` 时一次性应用——这样插件
    可以在导入期安全登记：

    .. code-block:: python

        from albuswall.configue import register_converter

        register_converter(
            QKeySequence,
            lambda raw: QKeySequence(raw),
            writer=lambda seq: seq.toString(QKeySequence.PortableText),
        )

    只有「构造函数语义不对」的类型才需要注册：普通 ``Enum`` / ``Path`` 子类 /
    带 ``X(str)`` 构造的类会被 ``_convert`` 的默认回退直接构造。

    Args:
        type_: 声明在字段注解里的类型
        reader: ``str -> 该类型实例``；抛异常会变成 ``ValueError``
        writer: ``实例 -> str``；不给则 INI 写回只能 ``str(value)``（不确定
            可逆时 ``flush`` 会抛 ``ValueError``，节点保持 dirty）
    """
    if Configue._instance is None:
        _PENDING_CONVERTERS.append((type_, reader, writer))
        return
    Configue._instance.resolver.register_converter(type_, reader, writer=writer)
