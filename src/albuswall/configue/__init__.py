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
``@dynamic_state`` 登记子树后，``config.dynamic.<name>`` 首次访问时从
``DYNAMIC_REGISTRY`` 懒加载；``setup_dynamic_config`` 只负责 import 模块。
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
    # utils
    "get_user_config_dir", "get_user_data_dir",
    "setup_static_config", "setup_dynamic_config", "load_config_tree",
    "parse_section_file",
    # state
    "ObservableNamespace", "StatefulNamespace", "StateField",
    "ObservableContext", "StateContext",
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

    # ── state ─────────────────────────────────────
    if name in (
            "ObservableNamespace", "StatefulNamespace", "StateField",
            "ObservableContext", "StateContext",
    ):
        from . import state
        return getattr(state, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
