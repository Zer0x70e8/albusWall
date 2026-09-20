#
"""
Trigger facade service: centrally manages scheduled triggers and device triggers,
loads configuration from the data repository, and distributes it to underlying services.
"""

import logging
import pprint
from typing import Dict, Optional, Callable, TYPE_CHECKING

from albuswall.dto.trigger import TriggerConfig
from albuswall.repositories.source import IngestSourceRepository
from .scheduler import SchedulerService
from .device import DeviceService
from ..common.enums import UpdateMode

if TYPE_CHECKING:
    from albuswall.log import Logger

_logger = logging.getLogger(__name__)
# noinspection statement-effect
_logger  # type: Logger
# noinspection unresolved-references
_logger.trace = lambda msg, *args, **kwargs: _logger.log(TRACE, msg, *args, **kwargs)


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

        # Initialize underlying services
        self._scheduler_service = SchedulerService(update_callback, _logger)
        self._device_service = DeviceService(update_callback, _logger)

        # Cache currently active configurations (source_id -> TriggerConfig)
        self._configs: Dict[int, TriggerConfig] = {}

    def start(self) -> None:
        """Load all configurations and start the underlying services."""
        _logger.info("Starting trigger facade service...")
        configs = self._load_valid_configs()
        self._configs = configs

        # Distribute configurations to the two underlying services
        scheduler_configs = self._filter_scheduler_configs()
        device_configs = self._filter_device_configs()

        self._scheduler_service.start(scheduler_configs)
        self._device_service.start(device_configs)
        _logger.info("Trigger facade service started")

    def stop(self) -> None:
        """Stop all underlying services."""
        _logger.info("Stopping trigger facade service...")
        self._scheduler_service.stop()
        self._device_service.stop()
        self._configs.clear()
        _logger.info("Trigger facade service stopped")

    def reload(self) -> None:
        """Reload configurations from the repository and update underlying services (for dynamic refresh)."""
        _logger.info("Reloading trigger configurations...")
        new_configs = self._load_valid_configs()

        old_ids = set(self._configs.keys())
        new_ids = set(new_configs.keys())

        # Remove deleted sources
        for source_id in old_ids - new_ids:
            self._scheduler_service.remove_source(source_id)
            self._device_service.remove_source(source_id)

        # Add new sources or update existing sources
        for source_id, config in new_configs.items():
            if source_id in old_ids:
                self._scheduler_service.update_source(source_id, config)
                self._device_service.update_source(source_id, config)
            else:
                self._scheduler_service.add_source(source_id, config)
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
        self._scheduler_service.add_source(source_id, config)
        self._device_service.add_source(source_id, config)

    def remove_source(self, source_id: int) -> None:
        """Manually remove the trigger configuration for the specified source."""
        self._configs.pop(source_id, None)
        self._scheduler_service.remove_source(source_id)
        self._device_service.remove_source(source_id)

    def update_source(self, source_id: int) -> None:
        """Manually update the trigger configuration for the specified source (re-read from repository)."""
        config = self._get_config(source_id)
        if config is None or config.is_void():
            # Configuration does not exist or is invalid; remove it
            self.remove_source(source_id)
            return
        self._configs[source_id] = config
        self._scheduler_service.update_source(source_id, config)
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
        """Read the trigger configuration for a single source from
        the repository (implemented by filtering all configurations)."""
        all_configs = self.repository.get_all_trigger_configs()
        for config in all_configs:
            if config.id == source_id:
                return config
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
