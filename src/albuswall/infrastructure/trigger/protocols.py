#
"""
触发器的协议定义。

本模块只声明“对外提供触发能力”所必需的接口；实现方自身的管理细节
（例如 APScheduler 的 job_id 分配、设备 target 映射的重建）不属于
协议的一部分，实现者可以自由添加。

关于 K
------
``K`` 是“触发主体”的标识符类型。它可以是 ``int``（例如 ingest source
id）、``str``（例如 UUID）、``uuid.UUID``，或任何可哈希值。门面与
handler 均不解释它，也不从 ``TriggerConfig`` 中推导它。
"""

from typing import Mapping, Protocol, TypeVar

from albuswall.dto.trigger import TriggerConfig

K = TypeVar("K")


class TriggerHandler(Protocol[K]):
    """所有触发器后端（定时 / 设备 / ...）共享的生命周期与配置接口。

    约定：
    * ``start`` / ``stop`` 必须幂等。
    * ``upsert`` 对未知 key 是新增，对已存在的 key 是替换
      （不得重复注册）。
    * ``remove`` 对不存在的 key 是 no-op。
    * 传入 ``start`` 的 configs 已经由调用方过滤为本 handler 关心的子集。
    """

    def start(self, configs: Mapping[K, TriggerConfig]) -> None:
        """使用给定配置启动 handler。"""
        ...

    def stop(self) -> None:
        """停止 handler 并释放资源。幂等。"""
        ...

    def upsert(self, key: K, config: TriggerConfig) -> None:
        """为 ``key`` 注册或替换一份配置。"""
        ...

    def remove(self, key: K) -> None:
        """注销 ``key`` 的配置；不存在时是 no-op。"""
        ...


class DeviceTriggerHandler(TriggerHandler[K], Protocol[K]):
    """设备触发器的协议（当前仓库不提供默认实现）。

    任何希望接入设备触发能力的实现（pyudev、平台专有 API、测试 fake），
    只要满足本协议，即可通过 ``TriggerFacadeService(device_handler=...)``
    注入使用。
    """
