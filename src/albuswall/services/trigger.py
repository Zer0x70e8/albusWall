#
"""
Trigger facade service: centrally manages scheduled triggers and device triggers,
loads configuration from the data repository, and distributes it to underlying services.
"""

import pprint
from typing import Dict, Optional, Callable

from albuswall.dto.trigger import TriggerConfig
from albuswall.repositories.source import IngestSourceRepository
from .scheduler import SchedulerService

from albuswall.log import getLogger

from ..common.enums import UpdateMode

from .device import DeviceService

_logger = getLogger(__name__)


class TriggerFacadeService:
    """Trigger facade responsible for configuration loading, distribution, and lifecycle management."""

    def __init__(
            self,
            repository: IngestSourceRepository,
            update_callback: Callable[[int], None],
    ):
        """
        :param repository: Repository used to read ingest source trigger configurations
        :param update_callback: Function called when a trigger update occurs; the parameter is source_id
        """
        self.repository = repository
        self.update_callback = update_callback

        self._started = False
        self._configs: Dict[int, TriggerConfig] = {}

        # Initialize underlying services
        self._scheduler_service = SchedulerService(update_callback, _logger)

        self._device_service = DeviceService(update_callback, _logger)

    def start(self) -> None:
        """Load all configurations and start the underlying services."""
        if self._started:
            _logger.warning("Trigger facade service is already started")
            return

        _logger.info("Starting trigger facade service...")
        try:
            configs = self._load_valid_configs()
        except Exception as e:
            _logger.error("Failed to load trigger configurations: %s", e, exc_info=True)
            raise

        self._configs = configs

        # Distribute configurations to the two underlying services
        scheduler_configs = self._filter_scheduler_configs()
        self._scheduler_service.start(scheduler_configs)

        device_configs = self._filter_device_configs()
        self._device_service.start(device_configs)

        self._started = True
        _logger.info("Trigger facade service started")

    def stop(self) -> None:
        """Stop all underlying services."""
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

    def reload(self) -> None:
        """Reload configurations from the repository and update underlying services (for dynamic refresh)."""
        _logger.info("Reloading trigger configurations...")
        try:
            new_configs = self._load_valid_configs()
        except Exception as e:
            _logger.error("Failed to reload trigger configurations, keeping old configs: %s", e, exc_info=True)
            return

        if not self._started:
            # Not started: only update cache
            self._configs = new_configs
            _logger.info("Configuration reloaded (service not started), %d valid sources", len(new_configs))
            return

        old_ids = set(self._configs.keys())
        new_ids = set(new_configs.keys())

        # Remove deleted sources
        for source_id in old_ids - new_ids:
            self._scheduler_service.remove_source(source_id)
            if self._device_service is not None:
                self._device_service.remove_source(source_id)

        # Add new sources or update existing sources
        for source_id, config in new_configs.items():
            if source_id in old_ids:
                self._scheduler_service.update_source(source_id, config)
                if self._device_service is not None:
                    self._device_service.update_source(source_id, config)
            else:
                self._scheduler_service.add_source(source_id, config)
                if self._device_service is not None:
                    self._device_service.add_source(source_id, config)

        self._configs = new_configs
        _logger.info("Configuration reload completed, %d valid sources in total", len(new_configs))

    def add_source(self, source_id: int) -> None:
        """Manually add a trigger configuration for the specified source (read from repository)."""
        config = self._get_config(source_id)
        if config is None or config.is_void():
            _logger.debug("Configuration for source %d is empty or invalid, skipping", source_id)
            return

        self._configs[source_id] = config
        if self._started:
            self._scheduler_service.add_source(source_id, config)
            if self._device_service is not None:
                self._device_service.add_source(source_id, config)

    def remove_source(self, source_id: int) -> None:
        """Manually remove the trigger configuration for the specified source."""
        self._configs.pop(source_id, None)
        if self._started:
            self._scheduler_service.remove_source(source_id)
            if self._device_service is not None:
                self._device_service.remove_source(source_id)

    def update_source(self, source_id: int) -> None:
        """Manually update the trigger configuration for the specified source (re-read from repository)."""
        config = self._get_config(source_id)
        if config is None or config.is_void():
            # Configuration does not exist or is invalid; remove it
            self.remove_source(source_id)
            return

        self._configs[source_id] = config
        if self._started:
            self._scheduler_service.update_source(source_id, config)
            if self._device_service is not None:
                self._device_service.update_source(source_id, config)

    # Internal helper methods
    def _load_valid_configs(self) -> Dict[int, TriggerConfig]:
        """
        Load trigger configurations for all ingest sources from the repository,
        filtering out invalid (is_void) configurations.
        Return a {source_id: TriggerConfig} dictionary.
        """
        configs_list = self.repository.get_all_trigger_configs()
        configs = {}
        for config in configs_list:
            if config is not None and not config.is_void():
                # The config.id returned by the repository is already set to source_id
                configs[config.id] = config
        _logger.debug("Valid trigger configs:\n%s", pprint.pformat(configs))
        return configs

    def _get_config(self, source_id: int) -> Optional[TriggerConfig]:
        """Read the trigger configuration for a single source from the repository."""
        try:
            return self.repository.get_trigger_config(source_id)
        except Exception as e:
            _logger.error("Failed to get trigger config for source %d: %s", source_id, e, exc_info=True)
            return None

    def _filter_scheduler_configs(self) -> Dict[int, TriggerConfig]:
        """Return the subset of configurations that only need the scheduler trigger."""
        return {
            sid: cfg for sid, cfg in self._configs.items()
            if (
                    cfg.scheduled.enabled
                    and (
                            UpdateMode.SCHEDULED_TIME in cfg.update_mode
                            or UpdateMode.INTERVAL_TIME in cfg.update_mode
                    )
            )
        }

    def _filter_device_configs(self) -> Dict[int, TriggerConfig]:
        """Return the subset of configurations that only need the device trigger."""
        return {
            sid: cfg for sid, cfg in self._configs.items()
            if (
                    cfg.device_trigger.enabled
                    and UpdateMode.DEVICE_TRIGGER in cfg.update_mode
                    and cfg.device_trigger.target is not None
            )
        }


# alias
TriggerService = TriggerFacadeService
