#
"""触发器桥接层：``ingest_source.trigger_config`` → ``TriggerFacadeService``。

* :class:`SourceTriggerService` —— 只做 inbound（源配置变更 → 触发器）。

本层**不** import ``state`` / ``runtime`` / ``command`` 任何子包；
出站链路（触发器到点 → 重扫）由装配层接线到
``SourceStateService.refresh_source``。
"""

from .service import SourceTriggerService

__all__ = ["SourceTriggerService"]
