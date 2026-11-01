#
"""源回收站的配置。

两个层次，与 ``service/old/import_.py`` 同构（同一套「声明 + 兜底」写法）：

1. **声明层** :class:`SourceTrashSettings` —— ``@configclass("source_trash",
   "main")``，默认值与类型在这里声明，是唯一事实来源。对应配置文件：

   .. code-block:: ini

       [source_trash]
       reconciler_name = source_trash
       priority        = 8
       debounce        = 1.0

   随包 ``conf.ini`` 里这一段已经按注释形式写好，取消注释即可覆盖。

2. **值对象层** :class:`SourceTrashConfig` —— frozen dataclass，服务只认它。
   服务不 import 配置系统、不读环境变量、不算路径（与 ``service/old/``
   的既有分工一致）。

``[source_trash]`` 段是**共享**的：本模块只声明源回收站自己消费的三个键
--------------------------------------------------------------------
``configue`` 支持同一个 ``(section, key)`` 被多个 ``@configclass`` 声明（它们
读写同一个 Resolver 节点，字段集合各声明各的），只要求**同名键的 type 与
default 等价**，否则装饰期就抛 ``ConfigDeclarationConflict``。

因此这一段切成两半、由两个域各自就近声明：

====================================  ==================================
类（所在模块）                         声明的键
====================================  ==================================
``SourceTrashSettings``（本模块）       ``reconciler_name`` / ``priority``
                                       / ``debounce``
``CleanerSettings``（thumbnail/cleaner） ``xdg_compat`` /
                                       ``orphan_min_age_seconds`` /
                                       ``session_marker_path`` /
                                       ``auto_cleanup_on_dirty``
====================================  ==================================

这样缩略图域不必 import 源回收站的配置模块（少一条跨域依赖边），谁把同名键
写歪了也会在启动期直接报错，而不是"后写者静默赢"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError
from albuswall.log import getLogger

__all__ = [
    "SourceTrashConfig",
    "SourceTrashSettings",
    "load_source_trash_config",
    "source_trash_config",
]

_logger = getLogger(__name__)


@configclass("source_trash", "main")
class SourceTrashSettings:
    """``[source_trash]`` 段里**源回收站**声明的那三个键。

    另外四个键由 ``service/thumbnail/cleaner.py`` 的 ``CleanerSettings``
    声明（同一 section 的多份声明共享同一个 Resolver 节点）。
    """

    #: 该服务在 Reconciler 里的注册名（``invalidate`` 时要指名道姓）。
    reconciler_name: str = "source_trash"

    #: 提交给 TaskService 的优先级：8 = 低于用户可见任务。
    priority: int = 8

    #: 一次 ``soft_delete`` 到真正 purge 之间的合并窗口（秒）。
    debounce: float = 1.0


#: 声明层字段名（顺序即 ``source_trash_config()`` 的快照顺序）。
_FIELDS: Tuple[str, ...] = ("reconciler_name", "priority", "debounce")


def _declared_defaults() -> Dict[str, Any]:
    """声明期默认值：``Configue`` 尚未构造时的兜底（单测 / 无配置环境）。

    必须走字段槽的 ``default``：类级属性访问拿到的是描述符本身，不是值
    （值只有在实例访问时才经 Resolver 解析）。
    """
    result: Dict[str, Any] = {}
    for name in _FIELDS:
        slot = getattr(SourceTrashSettings, name, None)
        result[name] = getattr(slot, "default", slot)
    return result


_SOURCE_TRASH_DEFAULTS: Dict[str, Any] = _declared_defaults()


def source_trash_config() -> Dict[str, Any]:
    """``[source_trash]`` 段里**源回收站**那三个键的快照。

    取值顺序：

    1. ``@configclass`` 声明的字段（``Configue`` 已构造 → 读配置文件 +
       声明默认值）；
    2. ``Configue`` 缺席 / 该段未加载 / 值非法时退回
       :data:`_SOURCE_TRASH_DEFAULTS`，并记一条 WARNING（**回退不静默**：
       否则「配置写了却被忽略」永远查不出来）。

    兜底范围与 ``import_.py`` 保持一致（``ConfigError`` + ``ValueError``）：
    前者是「配置系统不可用」，后者是 ``_convert`` 对非法值抛的裸异常。
    编程错误（``TypeError`` / ``AttributeError`` 等）**不兜**，当场炸。
    """
    try:
        settings = SourceTrashSettings()
        return {name: getattr(settings, name) for name in _FIELDS}
    except (ConfigError, ValueError) as exc:
        _logger.warning(
            "source_trash_config(): config system unavailable or invalid "
            "(%s: %s); falling back to declared defaults %s",
            type(exc).__name__, exc, _SOURCE_TRASH_DEFAULTS,
        )
        return dict(_SOURCE_TRASH_DEFAULTS)


@dataclass(frozen=True)
class SourceTrashConfig:
    """``SourceTrashService`` 的运行时配置（已经确定的值）。

    构造即校验：``debounce`` 不能为负、``reconciler_name`` 不能为空 ——
    这两条错的后果分别是「CPU 空转」和「invalidate 找不到条目」，
    都属于启动期就该炸的配置错误，而不是运行期静默失败。
    """

    reconciler_name: str = "source_trash"
    priority: int = 8
    debounce: float = 1.0

    def __post_init__(self) -> None:
        if self.debounce < 0:
            raise ValueError("debounce must be >= 0")
        if not self.reconciler_name:
            raise ValueError("reconciler_name must be non-empty")


def load_source_trash_config(
        conf: Dict[str, Any] | None = None,
) -> SourceTrashConfig:
    """把配置快照折叠成服务的值对象。"""
    if conf is None:
        conf = source_trash_config()
    return SourceTrashConfig(
        reconciler_name=conf["reconciler_name"],
        priority=conf["priority"],
        debounce=conf["debounce"],
    )
