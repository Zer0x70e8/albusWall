#
"""
Trigger facade service.

Pure infrastructure component that orchestrates an arbitrary number of
trigger backends. It does not read configuration from any repository, and
it does not assume what the "triggering subject" is: the identifier is a
generic type parameter supplied by the caller.
"""

import pprint
from typing import Callable, Dict, Generic, Mapping, Optional, TypeVar

from albuswall.dto.trigger import TriggerConfig
from albuswall.log import getLogger
from albuswall.common.enums import UpdateMode

from .protocols import DeviceTriggerHandler, TriggerHandler
from .scheduler import SchedulerService, SCHEDULED_MODES, active_modes

_logger = getLogger(__name__)

K = TypeVar("K")


class TriggerFacadeService(Generic[K]):
    """Trigger facade responsible for configuration distribution and lifecycle.

    Genericity
    ----------
    ``K`` is the type of the trigger subject identifier. It can be an
    ``int`` (e.g. an ingest source id), a ``str`` (e.g. a UUID), a
    ``uuid.UUID``, or any other hashable value. The facade never
    interprets it, and never derives it from ``TriggerConfig``.

    Data ownership
    --------------
    The facade owns no data source. All ``(key, TriggerConfig)`` pairs are
    pushed in by the caller via ``start`` / ``reload`` / ``upsert``. The
    facade keeps only an in-memory snapshot so it can compute diffs on
    reload. It never imports or calls a repository.

    Dependencies
    ------------
    The facade depends only on the ``TriggerHandler`` /
    ``DeviceTriggerHandler`` protocols defined in ``protocols.py``.
    ``SchedulerService`` is the default implementation for scheduled
    triggers; the device handler is always injected by the caller (no
    built-in implementation is shipped).
    """

    def __init__(
            self,
            update_callback: Callable[[K], None],
            *,
            scheduler_handler: Optional[TriggerHandler[K]] = None,
            device_handler: Optional[DeviceTriggerHandler[K]] = None,
    ) -> None:
        """
        :param update_callback: Called when a trigger fires; the parameter
            is the trigger subject key.
        :param scheduler_handler: Optional scheduled-trigger backend.
            Defaults to a ``SchedulerService`` bound to ``update_callback``.
        :param device_handler: Optional device-trigger backend. No default;
            pass one satisfying ``DeviceTriggerHandler`` when device
            triggers are needed.
        """
        self.update_callback = update_callback

        self._started = False
        self._configs: Dict[K, TriggerConfig] = {}

        self._scheduler_service: TriggerHandler[K] = (
            scheduler_handler
            if scheduler_handler is not None
            else SchedulerService(update_callback, _logger)
        )
        self._device_service: Optional[DeviceTriggerHandler[K]] = device_handler

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, configs: Mapping[K, TriggerConfig]) -> None:
        """Start the facade with the supplied configurations.

        Idempotent in the sense that a second call realigns state to the
        supplied configs rather than silently dropping them: it delegates
        to ``reload`` and diffs the previous snapshot against the new one.
        """
        if self._started:
            _logger.warning(
                "Trigger facade service is already started; "
                "reloading with the supplied configurations"
            )
            self.reload(configs)
            return

        _logger.info("Starting trigger facade service...")
        self._configs = self._index_configs(configs)

        self._scheduler_service.start(self._filter_scheduler_configs())
        if self._device_service is not None:
            self._device_service.start(self._filter_device_configs())

        self._started = True
        _logger.info(
            "Trigger facade service started with %d valid keys",
            len(self._configs),
        )

    def stop(self) -> None:
        """Stop all underlying services. Idempotent."""
        if not self._started:
            _logger.warning("Trigger facade service is not started")
            return

        _logger.info("Stopping trigger facade service...")
        self._scheduler_service.stop()
        if self._device_service is not None:
            self._device_service.stop()
        self._configs.clear()
        self._started = False
        _logger.info("Trigger facade service stopped")

    # ------------------------------------------------------------------ #
    # Data push API
    # ------------------------------------------------------------------ #
    def reload(self, configs: Mapping[K, TriggerConfig]) -> None:
        """Replace the whole configuration set and diff-apply it.

        * Not started: only the internal snapshot is updated.
        * Started: handlers receive precise upsert / remove calls based on
          the diff between the previous and the new snapshot.
        """
        _logger.info("Reloading trigger configurations...")
        new_configs = self._index_configs(configs)

        if not self._started:
            self._configs = new_configs
            _logger.info(
                "Configuration reloaded (service not started), %d valid keys",
                len(new_configs),
            )
            return

        old_keys = set(self._configs.keys())
        new_keys = set(new_configs.keys())

        # Removed keys
        for key in old_keys - new_keys:
            self._scheduler_service.remove(key)
            if self._device_service is not None:
                self._device_service.remove(key)

        # Added or updated keys
        for key, config in new_configs.items():
            self._scheduler_service.upsert(key, config)
            if self._device_service is not None:
                self._device_service.upsert(key, config)

        self._configs = new_configs
        _logger.info(
            "Configuration reload completed, %d valid keys in total",
            len(new_configs),
        )

    def upsert(self, key: K, config: Optional[TriggerConfig]) -> None:
        """Add or replace the trigger configuration for ``key``.

        * ``config is None``    -> no-op (nothing to say about the key).
        * ``config.is_void()``  -> remove the key.
        * otherwise             -> add or replace.
        """
        if config is None:
            return
        if config.is_void():
            self.remove(key)
            return

        self._configs[key] = config
        if self._started:
            self._scheduler_service.upsert(key, config)
            if self._device_service is not None:
                self._device_service.upsert(key, config)

    def remove(self, key: K) -> None:
        """Remove the trigger configuration for ``key``."""
        self._configs.pop(key, None)
        if self._started:
            self._scheduler_service.remove(key)
            if self._device_service is not None:
                self._device_service.remove(key)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_configs(
            configs: Mapping[K, TriggerConfig],
    ) -> Dict[K, TriggerConfig]:
        """Drop ``None`` and void configs, keep the rest keyed by ``K``."""
        if not configs:
            return {}
        indexed = {
            key: cfg
            for key, cfg in configs.items()
            if cfg is not None and not cfg.is_void()
        }
        _logger.debug("Indexed trigger configs:\n%s", pprint.pformat(indexed))
        return indexed

    def _filter_scheduler_configs(self) -> Dict[K, TriggerConfig]:
        """Subset of configs that want a scheduled job."""
        return {
            key: cfg for key, cfg in self._configs.items()
            if (
                    cfg.scheduled.enabled
                    and bool(active_modes(cfg) & SCHEDULED_MODES)
            )
        }

    def _filter_device_configs(self) -> Dict[K, TriggerConfig]:
        """Subset of configs that want a device trigger."""
        return {
            key: cfg for key, cfg in self._configs.items()
            if (
                    cfg.device_trigger.enabled
                    and UpdateMode.DEVICE_TRIGGER in active_modes(cfg)
                    and cfg.device_trigger.target is not None
            )
        }


# alias
TriggerService = TriggerFacadeService
