#
"""``SourceTriggerService`` —— 源配置 → 触发器门面的桥接层（只做 inbound）。

为什么需要这一层
----------------
``TriggerFacadeService`` 是**纯基础设施**：它不读仓储、不认识
``IngestSource``，只接受"键 + ``TriggerConfig``"。而"某个源的
``trigger_config`` 是什么"只有仓储知道。这条翻译缝就是本层。

边界
----
* facade 由容器注入 —— 本服务**不创建、不拥有、不 stop** 它
  （停止由 ``infrastructure/trigger/__init__.py`` 的 exit 钩子负责）；
* **只做 inbound**（源 → 触发器）。出站（触发器到点 → 重扫）由 facade
  构造时注入的 ``update_callback`` 负责，它固定发射
  ``trigger_refresh_signal``，再由装配层接到
  ``SourceStateService.refresh_source``；
* 不 import ``state`` / ``runtime`` / ``command`` 子包 —— 它只认三个
  ``Signal`` 对象，谁发射的不关心。

inbound 映射
------------
=============================  =========================================
信号 / 方法                    动作
=============================  =========================================
``source_created``             ``facade.upsert(id, _load_one(id))``
``source_changed``             ``facade.upsert(id, _load_one(id))``
``source_deleted``             ``facade.remove(id)``
:meth:`on_source_upserted`     同上（供回收站 ``restore`` 桥接）
:meth:`on_source_removed`      同上（供回收站 ``soft_delete`` 桥接）
=============================  =========================================
"""

from __future__ import annotations

import pprint
from typing import Dict, Optional

from albuswall.dto.trigger import TriggerConfig
from albuswall.log import getLogger
from albuswall.repositories.source import IngestSourceRepository
from albuswall.utils.signals import Signal

from albuswall.infrastructure.trigger.trigger import TriggerFacadeService

from ..errors import SourceNotFound

__all__ = ["SourceTriggerService"]

_logger = getLogger(__name__)


class SourceTriggerService:
    """把源配置信号桥接到 ``TriggerFacadeService``（仅 inbound 方向）。"""

    def __init__(
            self,
            repository: IngestSourceRepository,
            facade: TriggerFacadeService[int],
            *,
            source_created: Signal,
            source_changed: Signal,
            source_deleted: Signal,
    ) -> None:
        self._repository = repository
        self._facade = facade

        self._source_created = source_created
        self._source_changed = source_changed
        self._source_deleted = source_deleted

        self._started = False

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = f"SourceTriggerService(started={self._started})"
        if verbose:
            base = base[:-1] + f", facade={type(self._facade).__name__})"
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """全量加载触发器配置 → ``facade.start(configs)`` → 连三个信号。"""
        if self._started:
            _logger.warning("SourceTriggerService already started; ignored")
            return

        self._facade.start(self._load_all())

        self._source_created.connect(self._on_source_created)
        self._source_changed.connect(self._on_source_changed)
        self._source_deleted.connect(self._on_source_deleted)

        self._started = True
        _logger.info("SourceTriggerService started")

    def stop(self) -> None:
        """断开三个信号。幂等。

        **不**调 ``facade.stop()`` —— facade 归容器所有，由 trigger 基础设施
        的 exit 钩子负责停止（本服务只是它的一个 feeder）。
        """
        if not self._started:
            return
        self._source_created.disconnect(self._on_source_created)
        self._source_changed.disconnect(self._on_source_changed)
        self._source_deleted.disconnect(self._on_source_deleted)
        self._started = False
        _logger.info("SourceTriggerService stopped")

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------ 桥接（回收站）
    def on_source_upserted(self, source_id: int) -> None:
        """回收站 ``restore`` 的桥接入口：把该源的触发器配置推给 facade。

        Raises:
            SourceNotFound: 该源行已不存在（例如恢复与 purge 竞态）。
        """
        if not self._repository.exists(source_id):
            raise SourceNotFound(source_id)
        self._facade.upsert(source_id, self._load_one(source_id))

    def on_source_removed(self, source_id: int) -> None:
        """回收站 ``soft_delete`` 的桥接入口：撤掉该源的触发器。

        幂等：facade 对未登记的键是 no-op，因此"源已不存在"不是错误 ——
        与 :meth:`on_source_upserted` 不同，这里**不**校验行是否存在。
        """
        self._facade.remove(source_id)

    # ------------------------------------------------------------------ 信号槽
    def _on_source_created(self, _sender: object, *, source_id: int) -> None:
        self._facade.upsert(source_id, self._load_one(source_id))

    def _on_source_changed(self, _sender: object, *, source_id: int) -> None:
        # 配置可能被清空 —— ``_load_one`` 返回 ``None`` 时 facade 收到的是
        # "这个键没有触发器了"，语义上等价于 remove。
        self._facade.upsert(source_id, self._load_one(source_id))

    def _on_source_deleted(self, _sender: object, *, source_id: int) -> None:
        self._facade.remove(source_id)

    # ------------------------------------------------------------------ 配置加载
    def _load_all(self) -> Dict[int, TriggerConfig]:
        """全量读取触发器配置（跳过空配置），用于 ``facade.start``。"""
        configs_list = self._repository.get_all_trigger_configs()
        result: Dict[int, TriggerConfig] = {}
        for cfg in configs_list:
            if cfg is not None and not cfg.is_void():
                # noinspection bad-index
                result[cfg.id] = cfg
        _logger.debug("Loaded trigger configs:\n%s", pprint.pformat(result))
        return result

    # noinspection broad-exception
    def _load_one(self, source_id: int) -> Optional[TriggerConfig]:
        """单源触发器配置；解析失败只记日志并返回 ``None``。

        这里**吞异常**是刻意的：本方法跑在信号回调里，一条脏 ``trigger_config``
        不应该让"改个源标题"的写路径失败。严格语义请直接调
        ``IngestSourceRepository.get_trigger_config``。
        """
        try:
            return self._repository.get_trigger_config(source_id)
        except Exception:  # noqa: BLE001 —— 见 docstring
            _logger.exception(
                "Failed to load trigger config for source %d", source_id
            )
            return None
