#
"""统一配置系统（静态只读 + 动态可持久化）。

静态配置
========
``setup_static_config`` 把主 INI 及其 include 子树读进 ``Resolver``，
再经 ``ConfigDeclaration.load`` 注册类型、注入默认值，最后 ``check_all``。
``StaticConfig`` 是它的只读视图：``config.static.<section>.<key>``。

多文件加载
----------
主 INI 的 ``[configue]`` 段可声明子文件::

    [configue]
    include    = ./machine.ini, ./profile.d/*.ini
    include.ro = ./defaults.ini

合并顺序（低 → 高优先级，后者覆盖前者）：

1. ``include.ro`` 列出的文件（天然只读，``flush`` 不会改写它们）
2. ``include`` 列出的文件（文件系统不可写的视同只读）
3. 主文件自身

约定：逗号分隔；路径含 ``*`` / ``?`` / ``[`` 时按字典序 glob 展开；
相对路径相对主文件目录；子文件自己的 include 忽略（扁平结构，不递归）；
``pkg://`` 前缀暂未实现（抛 ``ConfigError``）。
每个生效值都记录 origin（``Resolver.origin_of``），供回写使用。
``[configue] include`` 系不会进入 ``StaticConfig``。

多文件约定的几条硬约束（踩过的坑）：

- include 路径中逗号是分隔符，路径里不能含逗号（``a,b.ini`` 会被拆成
  ``["a", "b.ini"]``）；含逗号的文件名请用 glob 匹配。
- ``_is_readonly`` 依赖 ``os.access(path, os.W_OK)``，在 root 下恒为真、
  判定失效；以 root 运行时必须用 ``include.ro`` 显式标记只读来源。
- ``_default_write_file`` 是「运行期新 key」与「只读来源且无可写备选」的
  唯一落点：``setup_static_config`` 会把它设成主 INI 路径；若主文件本身
  只读，需要显式 ``configure_persistence(default_write_file=...)`` 指到一个
  可写目录，否则 ``flush`` 白跑（错误记在 ``last_flush_error``）。
- ``flush`` 不检测外部修改（不做 mtime / 三方合并），单进程写回是约定；
  多进程同时改同一份 INI 会互相覆盖。
- ``[configue] disable_type_check = false``（默认）时 ``check_all`` 会对所有
  节点做类型转换，任一节点转换失败会让启动直接抛出（不是警告）。

类型转换
========
配置项声明的类型（``ConfigField`` / ``SettingItem.value_type``）由
``_convert`` 负责把 INI 字符串转成 Python 值，规则：

- 内置：``bool`` / ``int`` / ``float`` / ``str`` / ``Path`` / ``dict``（JSON 对象）
- 泛型递归：``Optional[X]`` / ``Union[...]`` / ``list[X]`` / ``tuple[X, ...]`` /
  ``dict[str, X]``，内层的 ``X`` 同样按下面的规则转换；``dict[str, list[X]]``
  这类嵌套容器的 value 也会递归
- **默认回退**：其余任何类型只要 ``X(raw)`` 能构造成功就直接可用，
  ``Enum`` / ``Path`` 子类 / ``str`` 子类 / 带 ``X(str)`` 构造的类**都不需要
  注册**；构造失败抛 ``ValueError``（带 raw 与 cause）
- **读写配对**：类型自带 ``from_ini(raw)`` 类方法 + ``to_ini()`` 方法时，
  读写两个方向都自动生效，完全不必注册
- 只有「构造函数语义不对」的类型才需要注册（``Namespace`` 子类、需要外部
  上下文的类），注册即等价于 ``configparser`` 的 ``converters``：

  .. code-block:: python

      from albuswall.configue import register_converter

      # reader 必需，writer 强烈建议成对给出
      register_converter(
          QKeySequence,
          lambda raw: QKeySequence(raw),
          writer=lambda seq: seq.toString(QKeySequence.PortableText),
      )
      # 或 config.register_converter(...)

  **只注册 reader 时写回只能 ``str(value)``**——``QKeySequence`` 就是反例：
  ``str()`` 在部分版本返回 NativeText（macOS 上是 ``⌘S``），写出去下次读不
  回来。所以这类类型必须给 ``writer``，或者实现 ``to_ini()``，或者干脆别
  直接当 ``value_type``。若 ``str(value)`` 只是默认 repr
  （``<... object at 0x...>``），``flush`` 直接抛 ``ValueError`` 而不是把
  坏值写进去。reader 抛异常变成 ``ValueError``（不再静默返回 ``None``）；
  writer 抛异常或返回非 ``str`` 同样抛 ``ValueError``（``flush`` 会包成
  ``ConfigError`` 并保留 dirty）。

写回时（``flush``）按同一套规则反解：``Enum`` 写 ``value``；序列默认写成
``", "`` 连接，但元素本身是容器（``list[list[int]]``）、或元素文本含逗号 /
首尾空白 / 是空串 / 以 ``[`` 开头时改写成 JSON 数组——否则
``["Ctrl+S", "Ctrl+,"]`` 会被逗号拆成三项，``Ctrl+,`` 直接丢数据。
读取侧与之对称：先试 JSON 数组，失败再按逗号分隔。

加载钩子
========
``ConfigField`` 只能声明「导入期就知道 key」的节点。像 ``[shortcuts]`` 这种
key（action_id）要到运行期才确定的段，用 ``register_load_hook`` 登记一个
``hook(resolver) -> None``：它在 ``ConfigDeclaration.load()`` 末尾执行
（类型声明之后、``check_all`` 之前），可以按段名批量补类型：

.. code-block:: python

    from albuswall.configue import register_load_hook

    register_load_hook(
        lambda resolver: [
            resolver.register_type("shortcuts", key, list[str])
            for key in resolver.keys_of("shortcuts")
        ]
    )

钩子只写内存、不读盘，导入期登记安全；宜在插件 ``setup()``（配置加载之前）
登记。配合 ``register_dynamic_state`` 的「先登记、后访问」模型，整个配置
phase 不需要 import 任何 UI / Qt。

回写
====
``config.set(section, key, value)`` 改内存并标脏，按持久化策略决定何时落盘：

- ``configure_persistence(autosave="atexit")``（默认）：退出时统一 flush
- ``autosave="debounce"``：set 后重置定时器，到期统一 flush（高频字段必用）
- ``autosave="off"`` / ``"manual"``：只标脏，全靠显式 ``flush()``
- ``save="now"`` 单次调用立即写

``config.remove(section, key)`` 是 ``set`` 的对称操作（同样的 ``save`` 时机）：
值立刻从内存消失（后续读取抛 ``KeyError``），但 origin 被保留——``flush()``
靠它找到「用户改过的那个文件」，让 ``ini_transform`` 删掉那一行（连同缩进
续行），不会留下残行。

``flush()`` 按 origin 分组，逐文件用 ``ini_transform.transform`` 生成编辑，
经 ``atomic_write`` 原子落盘，保留注释 / 格式 / 换行符。写盘失败不抛给业务：
记 ``last_flush_error``、保留 dirty，下次 flush 重试；``flush(strict=True)``
才抛 ``ConfigError(source=FILE)``。约定单进程写回，不做多进程保护，
也不做外部修改检测（mtime / 三方合并）。

动态配置
========
``config.dynamic.<name>`` 首次访问时从 ``DYNAMIC_REGISTRY`` 懒加载；
``setup_dynamic_config`` 只负责 import 模块。登记有两条路，都是**导入期安全**
的（只写登记表，类 / ``base_key`` / ``filename`` 到访问期才解析）：

**① 装饰器**——状态类所在的模块本身能在导入期被 import 时用：

.. code-block:: python

    from albuswall.configue import dynamic_state, StateField, StatefulNamespace

    @dynamic_state("user", ("path", "cache"), "user.json")
    class UserState(StatefulNamespace):
        user_id = StateField(str, default="anon")

**② 手动指定**——声明点比状态类更早被 import 时用（典型：UI 配置声明在插件
阶段就 import，UI 实现要等配置就绪）。``cls`` 可以写 ``"pkg.mod:Class"``，
访问期才 import：

.. code-block:: python

    from albuswall.configue import register_dynamic_state

    register_dynamic_state(
        "window",
        "albuswall.ui.builtin.presenter.window_presenter:WindowState",
        ("path", "cache"),
        WindowPresenterConfs.window_state_file_name,  # ConfigField → 取 .path
        autosave=False,
    )

``filename`` 三种写法（只有 str 是字面值，其余访问期解析）：

- ``"user.json"``：字面文件名；
- ``("files", "window_state")``：静态配置路径（1 或 2 段），访问期取值；
- ``WindowPresenterConfs.window_state_file_name``：直接传 ``ConfigField``，
  用它的 ``.path``。

配置 phase 统一导入（让 ``@dynamic_state`` 装饰器先跑过）：
``register_dynamic_module("pkg.mod")`` 在导入期登记，
``setup_dynamic_config(container)``（不传 ``modules``）会把登记过的模块全部
import；显式传 ``modules=[...]`` 时以显式为准。注意这跟 ② 的 ``cls`` 字符串是
两件事——后者只是延迟取类。

配置结构登记（软件级）
=====================
``setting_schema`` 把「分组 → 叶子 → ``(section, key)``」的层级结构登记到
``SETTING_SCHEMA``，不绑定任何前端；设置页、CLI、文档生成器都可以消费同一份
结构。登记是导入期安全的，dump / 回写时才解析当前值与归属文件：

.. code-block:: python

    from albuswall.configue import (
        SettingGroup, SettingItem, register_setting_group,
        format_setting_schema_dump, save_setting_group,
    )

    register_setting_group(
        SettingGroup("general", "常规").add(
            SettingItem("language", "general", "language", label="语言"),
        )
    )
    print(format_setting_schema_dump())
    save_setting_group("general", {"language": "en_US"})

``dump_setting_schema()`` 返回嵌套 dict（含 ``by_file`` 归属分组），
``dump_setting_schema_by_file()`` 只看文件维度；``save_*`` 会把编辑值按
``Item.file`` → ``Group.file`` → ``target_file_of`` 推出的归属文件回写。
"""

import sys as _sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar, Optional

from .configue import Configue
from .utils import (
    Namespace, FrozenNamespace,
    get_user_config_dir, get_user_data_dir,
    parse_section_file,
)

# ── 弃用标记 ────────────────────────────────────────────────────────────────
# 本包已由 ``albuswall.configue1`` 取代（迁移说明见仓库根 MIGRATION_CONFIGUE1.md）。
# 用 sys 上的标记保证「每进程只警告一次」——普通 import 本来只执行一次模块体，
# 但显式 ``importlib.reload`` 会重复执行，标记让那种情况也不刷屏。
_WARN_FLAG = "_albuswall_configue_deprecation_warned"
if not getattr(_sys, _WARN_FLAG, False):
    setattr(_sys, _WARN_FLAG, True)
    warnings.warn(
        "albuswall.configue is deprecated and will be removed; "
        "migrate to albuswall.configue1 (see MIGRATION_CONFIGUE1.md)",
        DeprecationWarning,
        stacklevel=2,
    )

T = TypeVar("T")

if TYPE_CHECKING:
    from .bootstrap import setup_dynamic_config, setup_static_config
    from .declaration import ConfigDeclaration, ConfigMeta, get_config
    from .dynamic_declaration import (
        DynamicMountSpec, dynamic_state, register_dynamic_module,
    )
    from .typed import load_typed

    # state.py 里全是运行期实现，直接 import 供类型检查器用即可；
    # StateField 是泛型描述符，IDE 对 __get__ 的返回类型解析得动，
    # 不需要像 ConfigField 那样写存根。
    from .state import (
        ObservableNamespace, StatefulNamespace, StateField,
        ObservableContext, StateContext,
    )


    class ConfigField(Generic[T]):
        """配置项描述符，自动根据解析配置。

        用法：
            x = ConfigField[int]("a", "b")       # x: int
            x = ConfigField("a", default=0)      # 由 default 推出 T=int
            x: int = ConfigField("a")            # 由变量注解推出
        """

        def __new__(
                cls,
                *path: str,
                default: T = ...,  # type: ignore[assignment]
                inject_default: bool = True,
        ) -> T: ...  # type: ignore[misc]


    # noinspection pep8-naming,unused-parameter
    def PathField(
            *path: str,
            default: Optional[Path | str] = None,
    ) -> Optional[Path]: ...

__all__ = [
    # config
    "Configue", "Namespace", "FrozenNamespace",
    "ConfigDeclaration", "ConfigField", "ConfigMeta", "get_config",
    "PathField",
    # typed dict 视图
    "load_typed",
    # utils
    "get_user_config_dir", "get_user_data_dir",
    "setup_static_config", "setup_dynamic_config", "load_config_tree",
    "parse_section_file",
    # state
    "ObservableNamespace", "StatefulNamespace", "StateField",
    "ObservableContext", "StateContext",
    # dynamic（导入期登记，和 ConfigField 同一层）
    "dynamic_state", "register_dynamic_state", "DynamicMountSpec",
    "register_dynamic_module",
    # 配置结构登记 / dump / 回写（软件级，前端无关）
    "SETTING_SCHEMA",
    "SettingGroup", "SettingItem", "SettingSchemaRegistry",
    "SettingWidget", "SettingWriteResult",
    "setting_group", "setting_item",
    "register_setting_group", "register_setting_item", "get_setting_group",
    "dump_setting_schema", "dump_setting_schema_by_file",
    "format_setting_schema_dump",
    "dump_registered_groups", "format_registered_groups",
    "save_setting_group", "save_setting_schema",
    # 自定义类型转换器（configparser 的 converters 同义）
    "register_converter",
    # 静态配置加载钩子（声明期枚举不出 key 的段用）
    "register_load_hook", "unregister_load_hook",
]


def register_converter(type_, converter, *, writer=None) -> None:
    """给静态配置注册一个自定义类型转换器（``configparser`` 的 ``converters``）。

    只有「构造函数语义不对」的类型才需要注册：普通 ``Enum`` / ``Path`` 子类 /
    ``int``、``str`` 子类 / 带 ``X(str)`` 构造的自定义类会被 ``_convert`` 的
    默认回退直接构造。典型需要注册的场景：``Namespace`` 子类（``X("raw")``
    不是你想要的）、需要外部上下文解析的类。

    ``writer`` 是写回方向（``实例 -> INI 文本``）。不传就只能 ``str(value)``，
    对 ``QKeySequence`` 这类 ``str()`` 不稳定的类型会把配置写坏，因此读写要
    成对注册：

    .. code-block:: python

        from albuswall.configue import register_converter

        register_converter(
            QKeySequence,
            lambda raw: QKeySequence(raw),
            writer=lambda seq: seq.toString(QKeySequence.PortableText),
        )

    或者让类型自带 ``from_ini`` / ``to_ini`` 方法，读写都无需注册。

    等价于 ``get_config().register_converter(...)``；注册后已缓存的节点会失效
    重转，因此可以在 ``setup_static_config`` 之后随时调用。
    """
    from .declaration import get_config

    get_config().register_converter(type_, converter, writer=writer)


def __getattr__(name):
    # ── bootstrap ─────────────────────────────────
    if name == "setup_static_config":
        from .bootstrap import setup_static_config
        return setup_static_config
    if name == "setup_dynamic_config":
        from .bootstrap import setup_dynamic_config
        return setup_dynamic_config
    if name == "load_config_tree":
        from .bootstrap import load_config_tree
        return load_config_tree
    if name == "setup_config":
        # 兼容旧 API：只跑静态阶段
        from .bootstrap import setup_static_config
        return setup_static_config

    # ── declaration ───────────────────────────────
    if name == "ConfigDeclaration":
        from .declaration import ConfigDeclaration
        return ConfigDeclaration
    if name == "PathField":
        from .declaration import PathField
        return PathField
    if name == "ConfigMeta":
        from .declaration import ConfigMeta
        return ConfigMeta
    if name == "ConfigField":
        from .declaration import ConfigField
        return ConfigField
    if name == "get_config":
        from .declaration import get_config
        return get_config
    if name in ("register_load_hook", "unregister_load_hook"):
        from . import declaration
        return getattr(declaration, name)

    # ── typed dict 视图 ───────────────────────────
    if name == "load_typed":
        from .typed import load_typed
        return load_typed

    # ── state ─────────────────────────────────────
    if name in (
            "ObservableNamespace", "StatefulNamespace", "StateField",
            "ObservableContext", "StateContext",
    ):
        from . import state
        return getattr(state, name)

    # ── dynamic（导入期登记，和 ConfigField 同一层）────
    if name in (
            "dynamic_state", "register_dynamic_state", "DynamicMountSpec",
            "register_dynamic_module",
            "DYNAMIC_REGISTRY", "DYNAMIC_MODULES", "reset_registry",
    ):
        from . import dynamic_declaration
        return getattr(dynamic_declaration, name)

    # ── 配置结构登记（软件级，前端无关）─────────────
    if name in (
            "SETTING_SCHEMA",
            "SettingGroup", "SettingItem", "SettingSchemaRegistry",
            "SettingWidget", "SettingWriteResult",
            "setting_group", "setting_item",
            "register_setting_group", "register_setting_item",
            "get_setting_group",
            "dump_setting_schema", "dump_setting_schema_by_file",
            "format_setting_schema_dump",
            "dump_registered_groups", "format_registered_groups",
            "save_setting_group", "save_setting_schema",
    ):
        from . import setting_schema
        return getattr(setting_schema, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
