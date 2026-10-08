#
"""
把源配置信号桥接到纯基础设施的 TriggerFacadeService。

职责：
  * 启动时全量加载触发器配置 → facade.start(configs)
  * 订阅源配置信号 source_added / source_updated / source_removed
    → facade.upsert / facade.remove

边界：
  * facade 由容器注入 —— 本服务不创建、不拥有、不 stop 它；
  * 出站链路（触发器到点 → 重扫）由 facade 的 update_callback 负责，
    本服务不参与；
  * 唯一允许 import IngestSourceRepository 的触发器相关组件；
  * trigger/ 包本身保持 repository-free。
"""

from __future__ import annotations

import pprint
from typing import Dict, Optional

from albuswall.dto.trigger import TriggerConfig
from albuswall.repositories.source import IngestSourceRepository
from albuswall.log import getLogger
from albuswall.utils.signals import Signal

from albuswall.infrastructure.trigger.trigger import TriggerFacadeService

_logger = getLogger(__name__)


class TriggerSyncService:
    """把源配置信号桥接到 TriggerFacadeService（仅 inbound 方向）。

    Inbound（源 → 触发器）
        ``source_added``   → ``facade.upsert``
        ``source_updated`` → ``facade.upsert``
        ``source_removed`` → ``facade.remove``

    出站（触发器 → 源）不在这里 —— 它由 facade 构造时注入的
    ``update_callback`` 负责（当前实现是 emit ``trigger_refresh_signal``，
    再由应用层接到 ``source_service.update_source``）。
    """

    def __init__(
            self,
            repository: IngestSourceRepository,
            facade: TriggerFacadeService[int],
            *,
            source_added: Signal,
            source_updated: Signal,
            source_removed: Signal,
    ) -> None:
        self._repository = repository
        self._facade = facade

        self._source_added = source_added
        self._source_updated = source_updated
        self._source_removed = source_removed

        self._started = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._started:
            _logger.warning("TriggerSyncService already started; ignored")
            return

        self._facade.start(self._load_all())

        self._source_added.connect(self._on_source_added)
        self._source_updated.connect(self._on_source_updated)
        self._source_removed.connect(self._on_source_removed)

        self._started = True
        _logger.info("TriggerSyncService started")

    def stop(self) -> None:
        if not self._started:
            return

        self._source_added.disconnect(self._on_source_added)
        self._source_updated.disconnect(self._on_source_updated)
        self._source_removed.disconnect(self._on_source_removed)

        # 注意：不要 self._facade.stop() —— facade 是容器拥有的，
        # 由 trigger/__init__.py 里的 _shutdown_trigger 负责停止。
        self._started = False
        _logger.info("TriggerSyncService stopped")

    # ------------------------------------------------------------------ #
    # Inbound：源配置变更 → 触发器
    # ------------------------------------------------------------------ #
    def _on_source_added(self, source_id: int) -> None:
        self._facade.upsert(source_id, self._load_one(source_id))

    def _on_source_updated(self, source_id: int) -> None:
        self._facade.upsert(source_id, self._load_one(source_id))

    def _on_source_removed(self, source_id: int) -> None:
        self._facade.remove(source_id)

    # ------------------------------------------------------------------ #
    # 内部：配置加载
    # ------------------------------------------------------------------ #
    def _load_all(self) -> Dict[int, TriggerConfig]:
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
        try:
            return self._repository.get_trigger_config(source_id)
        except Exception:
            _logger.exception(
                "Failed to load trigger config for source %d", source_id
            )
            return None
