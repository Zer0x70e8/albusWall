#
"""
Scheduler service: schedules timed tasks using APScheduler based on scheduled triggers
(scheduled_time / interval_time) in TriggerConfig.
"""

import pprint
from typing import Dict, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from albuswall.common.enums import UpdateMode
from albuswall.dto.trigger import TriggerConfig


class SchedulerService:
    """Scheduled trigger service based on APScheduler."""

    def __init__(self, update_callback: Callable[[int], None], logger):
        """
        :param update_callback: Function called when a trigger fires; the parameter is source_id
        """
        self.logger = logger
        self.update_callback = update_callback

        self._scheduler = BackgroundScheduler()
        # Map source_id -> job_id for dynamic management
        self._job_map: Dict[int, str] = {}

    def start(self, configs: Dict[int, TriggerConfig]) -> None:
        """
        Start the scheduler and load existing configurations.
        :param configs: Dictionary {source_id: TriggerConfig}
        """
        self.logger.info("Starting scheduler with %d configurations", len(configs))
        self._scheduler.start()
        for source_id, config in configs.items():
            self.add_source(source_id, config)

    def stop(self) -> None:
        """Stop the scheduler."""
        self.logger.info("Stopping scheduler")
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._job_map.clear()

    def add_source(self, source_id: int, config: TriggerConfig) -> None:
        """Add a scheduled job for an ingest source (if the configuration is valid)."""
        self.logger.debug(
            "Attempting to add scheduled job for source %d, config:\n%s",
            source_id,
            pprint.pformat(config),
        )
        if not self._should_schedule(config):
            self.logger.debug("Scheduled trigger is not enabled for source %d, skipping", source_id)
            return

        # Avoid duplicate addition
        if source_id in self._job_map:
            self.logger.warning("Scheduled job already exists for source %d; removing old job first", source_id)
            self.remove_source(source_id)

        job_id = f"ingest_source_{source_id}"
        try:
            if UpdateMode.SCHEDULED_TIME in config.update_mode:
                # Use CronTrigger (fixed time every day)
                cron_kwargs = config.scheduled.to_cron_trigger_kwargs()
                trigger = CronTrigger(**cron_kwargs)
                self.logger.debug(
                    "Source %d uses CronTrigger, kwargs:\n%s",
                    source_id,
                    pprint.pformat(cron_kwargs),
                )
            elif UpdateMode.INTERVAL_TIME in config.update_mode:
                # Use IntervalTrigger (fixed interval)
                interval_kwargs = config.scheduled.to_interval_trigger_kwargs()
                trigger = IntervalTrigger(**interval_kwargs)
                self.logger.debug(
                    "Source %d uses IntervalTrigger, kwargs:\n%s",
                    source_id,
                    pprint.pformat(interval_kwargs),
                )
            else:
                self.logger.error("Invalid scheduled trigger configuration for source %d", source_id)
                return

            self._scheduler.add_job(
                func=self._trigger_update,
                trigger=trigger,
                args=[source_id],
                id=job_id,
                replace_existing=True,
            )
            self._job_map[source_id] = job_id
            self.logger.info("Added scheduled job for source %d (job_id=%s)", source_id, job_id)
        except Exception as e:
            self.logger.error("Failed to add scheduled job for source %d: %s", source_id, e, exc_info=True)

    def remove_source(self, source_id: int) -> None:
        """Remove the scheduled job for an ingest source."""
        job_id = self._job_map.pop(source_id, None)
        if job_id and self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
            self.logger.info("Removed scheduled job for source %d", source_id)
        else:
            self.logger.debug("Source %d has no scheduled job or the job does not exist", source_id)

    def update_source(self, source_id: int, config: TriggerConfig) -> None:
        """Update the scheduled job for an ingest source (remove first, then add again)."""
        self.logger.info("Updating scheduled job for source %d", source_id)
        self.remove_source(source_id)
        self.add_source(source_id, config)

    @staticmethod
    def _should_schedule(config: TriggerConfig) -> bool:
        """Determine whether the scheduled trigger is enabled in the configuration."""
        return (
                config.scheduled.enabled
                and (
                        UpdateMode.SCHEDULED_TIME in config.update_mode
                        or UpdateMode.INTERVAL_TIME in config.update_mode
                )
        )

    def _trigger_update(self, source_id: int) -> None:
        """Trigger callback that performs the actual update operation."""
        self.logger.debug("Scheduled trigger fired; starting update for source %d", source_id)
        try:
            self.update_callback(source_id)
            self.logger.debug("Source %d update completed", source_id)
        except Exception as e:
            self.logger.exception("Error updating source %d: %s", source_id, e)
