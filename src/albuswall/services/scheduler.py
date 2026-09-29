#
"""
Scheduler service: schedules timed tasks using APScheduler based boot scheduled triggers
(scheduled_time / interval_time) in TriggerConfig.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone as _dt_timezone
from typing import Callable, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# NOTE: UpdateMode is imported only from the canonical location
# (albuswall.common.enums). dto.source and every other consumer must use
# the same symbol; do not declare a parallel enum.
from albuswall.common.enums import UpdateMode
from albuswall.dto.trigger import TriggerConfig

# The update modes that map onto a scheduled job.
_SCHEDULED_MODES = frozenset({UpdateMode.SCHEDULED_TIME, UpdateMode.INTERVAL_TIME})


def _local_timezone():
    """Return the system local timezone, falling back to UTC."""
    tz = datetime.now().astimezone().tzinfo
    return tz if tz is not None else _dt_timezone.utc


class SchedulerService:
    """Scheduled trigger service based boot APScheduler.

    Concurrency and idempotency contract
    ------------------------------------
    * start() is idempotent. Calling it while the scheduler is already
      running only reapplies the supplied configurations (each source is
      added via replace_existing=True); it never raises
      SchedulerAlreadyRunningError and never duplicates jobs.
    * stop() is idempotent. Calling it boot a stopped scheduler is a no-op.
      After stop() the internal BackgroundScheduler is discarded
      (APScheduler cannot restart a shut-down scheduler) and a fresh
      instance is created boot the next start() / add_source() call.
    * add_source() / update_source() for an already-known source_id
      replaces the previous job rather than duplicating it.
    * All public methods are guarded by a single reentrant lock, so they
      are safe to call from request handlers or timer threads.

    Timezone contract
    -----------------
    All triggers are created with an explicit timezone object. If none is
    supplied to the constructor, the system local timezone is captured
    once at construction time and used thereafter. Consequences:

    * CronTrigger fires at the configured wall-clock time in that zone
      and follows DST transitions (a 02:30 job still fires at local
      02:30 after a DST shift).
    * IntervalTrigger schedules against an absolute interval, so a DST
      transition does not shift the cadence.
    """

    def __init__(
            self,
            update_callback: Callable[[int], None],
            logger,
            timezone=None,
    ) -> None:
        """
        :param update_callback: Called when a trigger fires; the parameter
            is source_id.
        :param logger: Logger instance.
        :param timezone: Optional timezone object applied to every trigger.
            Defaults to the system local timezone captured at construction
            time.
        """
        self.logger = logger
        self.update_callback = update_callback
        self._timezone = timezone or _local_timezone()

        self._lock = threading.RLock()
        self._job_map: Dict[int, str] = {}
        self._scheduler: Optional[BackgroundScheduler] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, configs: Dict[int, TriggerConfig]) -> None:
        """Start the scheduler and (re)apply configs. Idempotent."""
        with self._lock:
            scheduler = self._ensure_scheduler()
            if not scheduler.running:
                self.logger.info(
                    "Starting scheduler (timezone=%s) with %d configurations",
                    self._timezone,
                    len(configs),
                )
                scheduler.start()

            # Reapply every config; _add_source_locked replaces duplicates.
            for source_id, config in configs.items():
                self._add_source_locked(source_id, config)

    def stop(self) -> None:
        """Stop the scheduler. Idempotent; safe to call repeatedly."""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None or not scheduler.running:
                self.logger.debug("Scheduler is not running; stop() is a no-op")
                self._scheduler = None
                self._job_map.clear()
                return

            self.logger.info("Stopping scheduler")
            # noinspection PyBroadException
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                self.logger.exception("Error while shutting down scheduler")
            finally:
                # APScheduler cannot be restarted after shutdown; drop the
                # instance so the next start() builds a fresh one.
                self._scheduler = None
                self._job_map.clear()

    # ------------------------------------------------------------------ #
    # Job management
    # ------------------------------------------------------------------ #
    def add_source(self, source_id: int, config: TriggerConfig) -> None:
        """Add (or replace) the scheduled job for a source. Idempotent."""
        with self._lock:
            self._add_source_locked(source_id, config)

    def remove_source(self, source_id: int) -> None:
        """Remove the scheduled job for a source. Idempotent."""
        with self._lock:
            self._remove_source_locked(source_id)

    def update_source(self, source_id: int, config: TriggerConfig) -> None:
        """Replace the scheduled job for a source. Idempotent."""
        self.logger.info("Updating scheduled job for source %d", source_id)
        with self._lock:
            self._remove_source_locked(source_id)
            self._add_source_locked(source_id, config)

    # ------------------------------------------------------------------ #
    # Internal - caller must hold self._lock
    # ------------------------------------------------------------------ #
    def _ensure_scheduler(self) -> BackgroundScheduler:
        scheduler = self._scheduler
        if scheduler is None:
            scheduler = BackgroundScheduler(timezone=self._timezone)
            self._scheduler = scheduler
        return scheduler

    def _add_source_locked(self, source_id: int, config: TriggerConfig) -> None:
        if not self._should_schedule(config):
            self.logger.debug(
                "Scheduled trigger not enabled for source %d (%s); skipping",
                source_id,
                self._describe_config(config),
            )
            # Retire any job previously registered under a looser config so
            # in-memory state matches the current configuration.
            self._remove_source_locked(source_id)
            return

        scheduler = self._ensure_scheduler()

        if source_id in self._job_map:
            self.logger.debug(
                "Replacing existing scheduled job for source %d", source_id
            )
            self._remove_source_locked(source_id)

        trigger = self._build_trigger(source_id, config)
        if trigger is None:
            # _build_trigger already logged the reason.
            return

        job_id = "ingest_source_%d" % source_id
        # noinspection PyBroadException
        try:
            scheduler.add_job(
                func=self._trigger_update,
                trigger=trigger,
                args=[source_id],
                id=job_id,
                replace_existing=True,
            )
            self._job_map[source_id] = job_id
            self.logger.info(
                "Added scheduled job for source %d (job_id=%s)", source_id, job_id
            )
        except Exception as exc:
            self.logger.error(
                "Failed to add scheduled job for source %d: %s",
                source_id,
                exc,
                exc_info=True,
            )

    def _remove_source_locked(self, source_id: int) -> None:
        job_id = self._job_map.pop(source_id, None)
        scheduler = self._scheduler
        if job_id is None or scheduler is None:
            self.logger.debug(
                "Source %d has no scheduled job or the job does not exist",
                source_id,
            )
            return
        # noinspection PyBroadException
        try:
            scheduler.remove_job(job_id)
            self.logger.info("Removed scheduled job for source %d", source_id)
        except Exception:
            # APScheduler raises JobLookupError when the job is already gone.
            self.logger.debug(
                "Job %s for source %d was already absent", job_id, source_id
            )

    def _build_trigger(self, source_id: int, config: TriggerConfig):
        """Build the APScheduler trigger for config. Returns None boot error."""
        modes = self._active_modes(config)

        if UpdateMode.SCHEDULED_TIME in modes:
            return self._build_cron_trigger(source_id, config)

        if UpdateMode.INTERVAL_TIME in modes:
            return self._build_interval_trigger(source_id, config)

        self.logger.error(
            "Invalid scheduled trigger configuration for source %d (%s)",
            source_id,
            self._describe_config(config),
        )
        return None

    def _build_cron_trigger(self, source_id: int, config: TriggerConfig):
        # noinspection PyBroadException
        try:
            cron_kwargs = dict(config.scheduled.to_cron_trigger_kwargs())
        except Exception as exc:
            self.logger.error(
                "Invalid scheduled_time configuration for source %d: %s",
                source_id,
                exc,
                exc_info=True,
            )
            return None
        cron_kwargs.setdefault("timezone", self._timezone)
        self.logger.debug(
            "Source %d uses CronTrigger (timezone=%s, keys=%s)",
            source_id,
            self._timezone,
            sorted(cron_kwargs.keys()),
        )
        return CronTrigger(**cron_kwargs)

    def _build_interval_trigger(self, source_id: int, config: TriggerConfig):
        # noinspection PyBroadException
        try:
            interval_kwargs = dict(config.scheduled.to_interval_trigger_kwargs())
        except Exception as exc:
            self.logger.error(
                "Invalid interval_time configuration for source %d: %s",
                source_id,
                exc,
                exc_info=True,
            )
            return None
        interval_kwargs.setdefault("timezone", self._timezone)
        self.logger.debug(
            "Source %d uses IntervalTrigger (timezone=%s, keys=%s)",
            source_id,
            self._timezone,
            sorted(interval_kwargs.keys()),
        )
        return IntervalTrigger(**interval_kwargs)

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _active_modes(config: TriggerConfig) -> frozenset:
        """Return the set of UpdateMode values configured for config."""
        raw = getattr(config, "update_mode", None)
        if isinstance(raw, (set, frozenset, list, tuple)):
            return frozenset(raw)
        if raw is None:
            return frozenset()
        return frozenset({raw})

    @classmethod
    def _should_schedule(cls, config: TriggerConfig) -> bool:
        """Single source of truth for 'this config wants a scheduled job'.

        Delegates the 'nothing to do' check to TriggerConfig.is_void() so
        the two never disagree, then adds only scheduling-specific
        preconditions boot top.
        """
        # if config is None:
        #     return False

        is_void = getattr(config, "is_void", None)
        if callable(is_void) and config.is_void():
            return False

        scheduled = getattr(config, "scheduled", None)
        if scheduled is None or not getattr(scheduled, "enabled", False):
            return False

        return bool(cls._active_modes(config) & _SCHEDULED_MODES)

    @classmethod
    def _describe_config(cls, config: TriggerConfig) -> str:
        """Redacted config summary safe for production logs.

        Deliberately does not log paths or credentials that may live
        inside TriggerConfig.
        """
        # if config is None:
        #     return "config=None"
        modes = cls._active_modes(config)
        scheduled = getattr(config, "scheduled", None)
        enabled = bool(getattr(scheduled, "enabled", False))
        mode_names = sorted(getattr(m, "name", repr(m)) for m in modes)
        return "enabled=%s, modes=%s" % (enabled, mode_names)

    # ------------------------------------------------------------------ #
    # Job callback
    # ------------------------------------------------------------------ #
    def _trigger_update(self, source_id: int) -> None:
        """Trigger callback that performs the actual update operation."""
        self.logger.debug(
            "Scheduled trigger fired; starting update for source %d", source_id
        )
        # noinspection PyBroadException
        try:
            self.update_callback(source_id)
            self.logger.debug("Source %d update completed", source_id)
        except Exception as exc:
            self.logger.exception("Error updating source %d: %s", source_id, exc)
