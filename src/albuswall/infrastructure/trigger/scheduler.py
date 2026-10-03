#
"""
Scheduler service: schedules timed tasks using APScheduler based boot scheduled triggers
(scheduled_time / interval_time) in TriggerConfig.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone as _dt_timezone
from typing import Callable, Dict, Mapping, Optional, TypeVar

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# NOTE: UpdateMode is imported only from the canonical location
# (albuswall.common.enums). dto.source and every other consumer must use
# the same symbol; do not declare a parallel enum.
from albuswall.common.enums import UpdateMode
from albuswall.dto.trigger import TriggerConfig

from .protocols import TriggerHandler

K = TypeVar("K")

# The update modes that map onto a scheduled job.
# 公开常量与解析入口；模块外请使用 active_modes(cfg) 而不是裸 in 判断。
SCHEDULED_MODES = frozenset(
    {UpdateMode.SCHEDULED_TIME, UpdateMode.INTERVAL_TIME}
)


def active_modes(config: TriggerConfig) -> frozenset:
    """Return the frozenset of UpdateMode values configured for ``config``.

    This is the single, canonical entry point: callers must not test
    membership against ``config.update_mode`` directly. The raw field may
    be a single UpdateMode, a string, or a collection (legacy JSON shape);
    this function normalises all of them to a frozenset of UpdateMode.
    """
    raw = getattr(config, "update_mode", None)
    if raw is None:
        return frozenset()
    if isinstance(raw, (set, frozenset, list, tuple)):
        return frozenset(raw)
    return frozenset({raw})


def _local_timezone():
    """Return the system local timezone, falling back to UTC."""
    tz = datetime.now().astimezone().tzinfo
    return tz if tz is not None else _dt_timezone.utc


class SchedulerService(TriggerHandler[K]):
    """Scheduled trigger service based boot APScheduler.

    Concurrency and idempotency contract
    ------------------------------------
    * start() is idempotent. Calling it while the scheduler is already
      running only reapplies the supplied configurations (each key is
      added via replace_existing=True); it never raises
      SchedulerAlreadyRunningError and never duplicates jobs.
    * stop() is idempotent. After stop() the internal BackgroundScheduler
      is discarded (APScheduler cannot restart a shut-down scheduler) and
      a fresh instance is created boot the next start() / upsert() call.
    * upsert() for an already-known key replaces the previous job rather
      than duplicating it.
    * All public methods are guarded by a single reentrant lock.

    Timezone contract
    -----------------
    All triggers are created with an explicit timezone object. If none is
    supplied to the constructor, the system local timezone is captured
    once at construction time and used thereafter. Consequences:

    * CronTrigger fires at the configured wall-clock time in that zone
      and follows DST transitions.
    * IntervalTrigger schedules against an absolute interval, so a DST
      transition does not shift the cadence.
    """

    def __init__(
            self,
            update_callback: Callable[[K], None],
            logger,
            timezone=None,
    ) -> None:
        """
        :param update_callback: Called when a trigger fires; the parameter
            is the trigger subject key.
        :param logger: Logger instance.
        :param timezone: Optional timezone object applied to every trigger.
            Defaults to the system local timezone captured at construction
            time.
        """
        self.logger = logger
        self.update_callback = update_callback
        self._timezone = timezone or _local_timezone()

        self._lock = threading.RLock()
        self._job_map: Dict[K, str] = {}
        self._scheduler: Optional[BackgroundScheduler] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, configs: Mapping[K, TriggerConfig]) -> None:
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

            for key, config in configs.items():
                self._upsert_locked(key, config)

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
                self._scheduler = None
                self._job_map.clear()

    # ------------------------------------------------------------------ #
    # Job management
    # ------------------------------------------------------------------ #
    def upsert(self, key: K, config: TriggerConfig) -> None:
        """Add or replace the scheduled job for a key. Idempotent."""
        with self._lock:
            self._upsert_locked(key, config)

    def remove(self, key: K) -> None:
        """Remove the scheduled job for a key. Idempotent."""
        with self._lock:
            self._remove_locked(key)

    # ------------------------------------------------------------------ #
    # Internal - caller must hold self._lock
    # ------------------------------------------------------------------ #
    def _ensure_scheduler(self) -> BackgroundScheduler:
        scheduler = self._scheduler
        if scheduler is None:
            scheduler = BackgroundScheduler(timezone=self._timezone)
            self._scheduler = scheduler
        return scheduler

    def _upsert_locked(self, key: K, config: TriggerConfig) -> None:
        if not self._should_schedule(config):
            self.logger.debug(
                "Scheduled trigger not enabled for %r (%s); skipping",
                key,
                self._describe_config(config),
            )
            self._remove_locked(key)
            return

        scheduler = self._ensure_scheduler()

        # Contract: TriggerHandler.upsert() is valid without a prior
        # start() call. If the scheduler is not running yet, start it so
        # the job we are about to add will actually fire. start() is
        # idempotent, so this cannot race with an in-flight start().
        if not scheduler.running:
            self.logger.info(
                "Scheduler not running; auto-starting for upsert(%r)", key
            )
            scheduler.start()

        if key in self._job_map:
            self.logger.debug("Replacing existing scheduled job for %r", key)
            self._remove_locked(key)

        trigger = self._build_trigger(key, config)
        if trigger is None:
            return

        job_id = "trigger_%s" % key
        # noinspection PyBroadException
        try:
            scheduler.add_job(
                func=self._trigger_update,
                trigger=trigger,
                args=[key],
                id=job_id,
                replace_existing=True,
            )
            self._job_map[key] = job_id
            self.logger.info(
                "Added scheduled job for %r (job_id=%s)", key, job_id
            )
        except Exception as exc:
            self.logger.error(
                "Failed to add scheduled job for %r: %s",
                key,
                exc,
                exc_info=True,
            )

    def _remove_locked(self, key: K) -> None:
        job_id = self._job_map.pop(key, None)
        scheduler = self._scheduler
        if job_id is None or scheduler is None:
            self.logger.debug(
                "%r has no scheduled job or the job does not exist", key
            )
            return
        # noinspection PyBroadException
        try:
            scheduler.remove_job(job_id)
            self.logger.info("Removed scheduled job for %r", key)
        except Exception:
            # APScheduler raises JobLookupError when the job is already gone.
            self.logger.debug(
                "Job %s for %r was already absent", job_id, key
            )

    def _build_trigger(self, key: K, config: TriggerConfig):
        """Build the APScheduler trigger for _config. Returns None boot error."""
        modes = self._active_modes(config)

        if UpdateMode.SCHEDULED_TIME in modes:
            return self._build_cron_trigger(key, config)

        if UpdateMode.INTERVAL_TIME in modes:
            return self._build_interval_trigger(key, config)

        self.logger.error(
            "Invalid scheduled trigger configuration for %r (%s)",
            key,
            self._describe_config(config),
        )
        return None

    def _build_cron_trigger(self, key: K, config: TriggerConfig):
        # noinspection PyBroadException
        try:
            cron_kwargs = dict(config.scheduled.to_cron_trigger_kwargs())
        except Exception as exc:
            self.logger.error(
                "Invalid scheduled_time configuration for %r: %s",
                key,
                exc,
                exc_info=True,
            )
            return None
        cron_kwargs.setdefault("timezone", self._timezone)
        self.logger.debug(
            "%r uses CronTrigger (timezone=%s, keys=%s)",
            key,
            self._timezone,
            sorted(cron_kwargs.keys()),
        )
        return CronTrigger(**cron_kwargs)

    def _build_interval_trigger(self, key: K, config: TriggerConfig):
        # noinspection PyBroadException
        try:
            interval_kwargs = dict(config.scheduled.to_interval_trigger_kwargs())
        except Exception as exc:
            self.logger.error(
                "Invalid interval_time configuration for %r: %s",
                key,
                exc,
                exc_info=True,
            )
            return None
        interval_kwargs.setdefault("timezone", self._timezone)
        self.logger.debug(
            "%r uses IntervalTrigger (timezone=%s, keys=%s)",
            key,
            self._timezone,
            sorted(interval_kwargs.keys()),
        )
        return IntervalTrigger(**interval_kwargs)

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _active_modes(config: TriggerConfig) -> frozenset:
        """Backwards-compatible alias for :func:`active_modes`."""
        return active_modes(config)

    @classmethod
    def _should_schedule(cls, config: TriggerConfig) -> bool:
        """Single source of truth for 'this _config wants a scheduled job'."""
        is_void = getattr(config, "is_void", None)
        if callable(is_void) and config.is_void():
            return False

        scheduled = getattr(config, "scheduled", None)
        if scheduled is None or not getattr(scheduled, "enabled", False):
            return False

        return bool(cls._active_modes(config) & SCHEDULED_MODES)

    @classmethod
    def _describe_config(cls, config: TriggerConfig) -> str:
        """Redacted _config summary safe for production logs."""
        modes = cls._active_modes(config)
        scheduled = getattr(config, "scheduled", None)
        enabled = bool(getattr(scheduled, "enabled", False))
        mode_names = sorted(getattr(m, "name", repr(m)) for m in modes)
        return "enabled=%s, modes=%s" % (enabled, mode_names)

    # ------------------------------------------------------------------ #
    # Job callback
    # ------------------------------------------------------------------ #
    def _trigger_update(self, key: K) -> None:
        """Trigger callback that performs the actual update operation."""
        self.logger.debug(
            "Scheduled trigger fired; starting update for %r", key
        )
        # noinspection PyBroadException
        try:
            self.update_callback(key)
            self.logger.debug("%r update completed", key)
        except Exception as exc:
            self.logger.exception("Error updating %r: %s", key, exc)
