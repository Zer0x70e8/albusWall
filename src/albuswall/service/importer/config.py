#
"""导入服务的配置声明与快照。

两层，与 ``service/source/trash/config.py`` 同构：

1. **声明层** :class:`ImportConfig` —— ``@configclass("import", "main")``，
   默认值与类型在这里声明，是唯一事实来源：

   .. code-block:: ini

       [import]
       batch_size      = 500     # 单轮 pending 批量上限
       process_timeout = 600.0   # 单任务等待上限（秒）
       initial_backoff = 1.0     # worker 异常后首次退避（秒）
       max_backoff     = 60.0    # worker 退避封顶（秒）

2. **快照层** :class:`ImportServiceConfig` —— typed dict 形状，
   ``import_config()`` 一次取出。

为什么快照是 typed dict 而不是 dataclass：取值点都在 worker 的**热路径**上
（每轮 ``process_pending`` 读一次 ``batch_size``），dict 读取比属性链短；
且它同时是"给进程池用的纯数据"，不需要类型校验。

``process_timeout`` 的真实语义（**别误读**）：``ProcessPoolExecutor`` 没法
真正中断子进程，超时只是让主线程不再等、并把候选标成失败。想「超时即 kill」
得换成显式 ``multiprocessing.Process`` + ``join(timeout)`` + ``terminate()``，
或引入 pebble 之类的库。当前实现里超时后子进程**仍在跑**，只是结果被丢弃。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple, TypedDict

from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError
from albuswall.log import getLogger

__all__ = [
    "ImportConfig",
    "ImportServiceConfig",
    "import_config",
]

_logger = getLogger(__name__)


class ImportServiceConfig(TypedDict):
    """``ImportService`` 的可调参数快照（每轮 ``process_pending`` 读一次）。"""

    #: 单轮 pending 批量上限
    batch_size: int
    #: 单个候选任务在进程池里的等待上限（秒）
    process_timeout: float
    #: worker 异常后首次退避（秒）
    initial_backoff: float
    #: worker 退避封顶（秒）
    max_backoff: float


@configclass("import", "main")
class ImportConfig:
    """``[import]`` 段的声明（默认值 = 随包 ``conf.ini`` 里的注释值）。"""

    #: 单轮 pending 批量上限。
    batch_size: int = 500

    #: 单个候选任务在进程池里的等待上限（秒）；超时由主线程取消 + 标失败。
    process_timeout: float = 600.0

    #: worker 异常后首次退避（秒）；连续失败按 2 倍指数增长。
    initial_backoff: float = 1.0

    #: worker 退避上限（秒）。
    max_backoff: float = 60.0


#: 声明层字段名（顺序即快照顺序）。
_FIELDS: Tuple[str, ...] = (
    "batch_size", "process_timeout", "initial_backoff", "max_backoff",
)


def _declared_defaults() -> Dict[str, Any]:
    """声明期默认值：``Configue`` 尚未构造时的兜底（单测 / 无配置环境）。

    必须走字段槽的 ``default``：类级属性访问拿到的是描述符本身，不是值。
    """
    result: Dict[str, Any] = {}
    for name in _FIELDS:
        slot = getattr(ImportConfig, name, None)
        result[name] = getattr(slot, "default", slot)
    return result


_IMPORT_DEFAULTS: Dict[str, Any] = _declared_defaults()


def import_config() -> ImportServiceConfig:
    """``[import]`` 段的快照。

    取值顺序：

    1. ``@configclass`` 声明的字段（``Configue`` 已构造 → 读配置文件 +
       声明默认值）；
    2. ``Configue`` 缺席 / 该段未加载 / 值非法时退回
       :data:`_IMPORT_DEFAULTS`，并记一条 WARNING（**回退不静默**：
       否则「配置写了却被忽略」永远查不出来）。

    兜底的异常范围（刻意收窄）：

    * ``ConfigError`` —— 覆盖三种"配置系统不可用"：``Configue`` 未构造
      （``ConfigMissingError``）、类不在登记表（``ConfigClassNotRegistered``）、
      节点无值（``ConfigMissingError``）；
    * ``ValueError`` —— 配置值非法。``configue`` 的 ``_convert`` 对
      「``batch_size = abc``」这类输入抛的是裸 ``ValueError``，它不是
      ``ConfigError``；本方法在 import worker 线程的循环入口被调用，漏掉它 =
      worker 线程**静默死亡**、导入链路整体停摆。

    其余异常（``TypeError`` / ``AttributeError`` / 任意 ``RuntimeError``）
    一律**不兜**：那是调用方的编程错误，应当当场炸出来而不是被默认值掩盖。
    """
    try:
        config = ImportConfig()
        return {
            name: getattr(config, name) for name in _FIELDS
        }  # type: ignore[return-value]
    except (ConfigError, ValueError) as exc:
        _logger.warning(
            "import_config(): config system unavailable or invalid (%s: %s); "
            "falling back to declared defaults %s",
            type(exc).__name__, exc, _IMPORT_DEFAULTS,
        )
        return dict(_IMPORT_DEFAULTS)  # type: ignore[return-value]
