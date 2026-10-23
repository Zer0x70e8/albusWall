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

回写
====
``config.set(section, key, value)`` 改内存并标脏，按持久化策略决定何时落盘：

- ``configure_persistence(autosave="atexit")``（默认）：退出时统一 flush
- ``autosave="debounce"``：set 后重置定时器，到期统一 flush（高频字段必用）
- ``autosave="off"`` / ``"manual"``：只标脏，全靠显式 ``flush()``
- ``save="now"`` 单次调用立即写

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

from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar, Optional

from .configue import Configue
from .utils import (
    Namespace, FrozenNamespace,
    get_user_config_dir, get_user_data_dir,
    parse_section_file,
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
]


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
