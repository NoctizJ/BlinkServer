#!/usr/bin/env python3
"""Run one job at a set time every weekday, from inside the server process.

The schedule is a small JSON object in ``configs/schedule.json``::

    {
      "time": "06:30",                   # HH:MM, 24-hour, in "timezone"
      "timezone": "America/Los_Angeles",
      "weekdays_only": true,             # optional, default true: skip Saturday and Sunday
      "catch_up_until": "12:00"          # optional, see below
    }

``app.py`` hands the job function to :func:`start_scheduler`; it is called with an
empty payload dict. The job only runs while its switch is on: the switch is the name
of the function's module (e.g. ``qqqholdings_job``) in ``job_switches.json``, the same
one the webhook for that module honours. A job that is switched off at its slot is
skipped for that day.

A run counts as done when the function returns without raising and without
``{"status": "error"}``; that day is then recorded in ``state/scheduler.json``.

**Catch-up window.** ``catch_up_until`` is the time of day after which a missed or
failed run is no longer worth making. Inside the window, on a run day, with the job
not yet done today:

  * if the server starts late, it runs the job once at startup;
  * if the job fails, it is tried again every ``RETRY_AFTER`` until it succeeds or
    the window closes;
  * if the Mac slept through the slot, the job runs when it wakes. Outside the
    window, or on a day that is not a run day, the missed slot is skipped and the
    next run is the next slot.

Without ``catch_up_until`` none of this applies: a slot missed while the Mac slept
runs late, when the clock is next checked; a slot missed while the server was down
is not made up for; and a failure waits for the next slot.

The clock is checked every ``TICK_SECONDS`` rather than slept until the exact moment.
"""

import dataclasses
import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Dict, Optional
from zoneinfo import ZoneInfo

from jobs.log_engine import log as write_log
from jobs.switches import master_enabled

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = REPO_ROOT / "configs" / "schedule.json"
STATE_PATH = REPO_ROOT / "state" / "scheduler.json"
TICK_SECONDS = 30
RETRY_AFTER = datetime.timedelta(minutes=15)
LOG_TYPE = "scheduler"
SCHEDULE_KEYS = frozenset({"time", "timezone", "weekdays_only", "catch_up_until"})


def _log(text: str) -> None:
    """Log without letting a logging problem break the scheduler thread."""
    try:
        write_log(LOG_TYPE, text)
    except Exception:
        logger.exception("could not write the scheduler log entry")


def _parse_hhmm(text: str, field: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(str(text), "%H:%M").time()
    except ValueError:
        raise ValueError(f"{field} must look like HH:MM (24-hour), got {text!r}") from None


@dataclasses.dataclass
class Schedule:
    run_time: datetime.time
    tz: ZoneInfo
    weekdays_only: bool
    catch_up_until: Optional[datetime.time]

    @classmethod
    def from_config(cls, raw: dict) -> "Schedule":
        """Build and validate the schedule; raise ValueError on a bad one."""
        if not isinstance(raw, dict):
            raise ValueError(f"the schedule must be an object, got {raw!r}")
        unknown = sorted(set(raw) - SCHEDULE_KEYS)
        if unknown:
            raise ValueError(f"unknown schedule key(s) {unknown}; allowed: {sorted(SCHEDULE_KEYS)}")
        missing = [key for key in ("time", "timezone") if not raw.get(key)]
        if missing:
            raise ValueError(f"the schedule is missing {missing}")
        weekdays_only = raw.get("weekdays_only", True)
        if not isinstance(weekdays_only, bool):
            raise ValueError(f"weekdays_only must be true or false, got {weekdays_only!r}")
        try:
            tz = ZoneInfo(raw["timezone"])
        except Exception:
            raise ValueError(f"unknown timezone {raw['timezone']!r}") from None
        cutoff = raw.get("catch_up_until")
        return cls(
            run_time=_parse_hhmm(raw["time"], "time"),
            tz=tz,
            weekdays_only=weekdays_only,
            catch_up_until=_parse_hhmm(cutoff, "catch_up_until") if cutoff else None,
        )


def load_schedule(path=None) -> Optional[Schedule]:
    """Read and validate configs/schedule.json (a missing file means nothing is scheduled)."""
    try:
        with open(path or SCHEDULE_PATH, encoding="utf-8") as f:
            return Schedule.from_config(json.load(f))
    except FileNotFoundError:
        return None


def next_run_after(now: datetime.datetime, run_time: datetime.time, tz: ZoneInfo,
                   weekdays_only: bool = True) -> datetime.datetime:
    """The first ``run_time`` in ``tz`` strictly after ``now`` (on a weekday, if asked).

    Each candidate is built from a calendar date and a wall-clock time, never by
    adding 24 hours, so it stays at 06:30 local across a daylight-saving change.
    ``candidate > now`` compares instants, so in the repeated hour of the autumn change
    a slot that has already passed is never returned as the next run.
    """
    day = now.astimezone(tz).date()
    while True:
        candidate = datetime.datetime.combine(day, run_time, tzinfo=tz)
        if candidate > now and (not weekdays_only or day.weekday() < 5):
            return candidate
        day += datetime.timedelta(days=1)


class Scheduler:
    """Runs ``func`` once a day on a Schedule. ``name``, ``clock`` and ``state_path`` are injectable."""

    def __init__(self, func: Callable[[dict], object], schedule: Schedule,
                 name: Optional[str] = None,
                 clock: Optional[Callable[[], datetime.datetime]] = None,
                 state_path: Optional[Path] = None):
        self.func = func
        self.schedule = schedule
        self.name = name or func.__module__.split(".")[-1]  # the job switch, and the key in the state file
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._due = next_run_after(self.clock(), schedule.run_time, schedule.tz, schedule.weekdays_only)
        self._retry_at: Optional[datetime.datetime] = None
        self._stop = threading.Event()

    # -- state ---------------------------------------------------------------
    def _last_runs(self) -> Dict[str, str]:
        try:
            runs = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # missing, unreadable, corrupt or not UTF-8
            return {}
        return runs if isinstance(runs, dict) else {}

    def _record_run(self, day: datetime.date) -> None:
        runs = self._last_runs()
        runs[self.name] = day.isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(runs, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # -- running -------------------------------------------------------------
    def _fire(self, now: datetime.datetime) -> Optional[bool]:
        """Run the job: True if it finished, False if it failed, None if switched off."""
        if not master_enabled(self.name):
            _log(f"{self.name}: skipped, the job is switched off")
            return None
        try:
            result = self.func({})
        except (Exception, SystemExit) as exc:  # a failing job must never kill the thread
            logger.exception("scheduled job %s raised", self.name)
            _log(f"{self.name}: raised {type(exc).__name__}: {exc}")
            return False
        if isinstance(result, dict) and result.get("status") == "error":
            _log(f"{self.name}: reported an error: {result.get('message')}")
            return False
        try:
            self._record_run(now.astimezone(self.schedule.tz).date())
        except OSError as exc:  # the job did run; a full disk must not make it run again
            logger.exception("could not record the run of %s", self.name)
            _log(f"{self.name}: ran, but could not record it: {exc}")
        status = result.get("status") if isinstance(result, dict) else "done"
        _log(f"{self.name}: ran ({status})")
        return True

    def _run(self, now: datetime.datetime) -> bool:
        """Fire, and if the job failed, try again RETRY_AFTER later (inside the window)."""
        ok = self._fire(now)
        if ok is False and self.schedule.catch_up_until is not None:
            self._retry_at = now + RETRY_AFTER
        return bool(ok)

    def _catch_up_due(self, now: datetime.datetime) -> bool:
        """True on a run day, inside [time, catch_up_until), when today's run is not done."""
        schedule = self.schedule
        if schedule.catch_up_until is None:
            return False
        local = now.astimezone(schedule.tz)
        if schedule.weekdays_only and local.weekday() >= 5:
            return False
        if not (schedule.run_time <= local.time() < schedule.catch_up_until):
            return False
        return self._last_runs().get(self.name) != local.date().isoformat()

    def catch_up(self) -> None:
        """Run the job now if its time already passed today and it has not run."""
        now = self.clock()
        if self._catch_up_due(now):
            _log(f"{self.name}: catching up a missed run")
            self._run(now)

    def tick(self) -> bool:
        """Run the job if its slot or a retry has come due; True if it ran to completion."""
        now = self.clock()
        if now >= self._due:
            # Move the due time first: whatever happens while the job runs, it must not
            # fire again until its next slot.
            self._due = next_run_after(now, self.schedule.run_time, self.schedule.tz,
                                       self.schedule.weekdays_only)
            self._retry_at = None
            if self.schedule.catch_up_until is not None and not self._catch_up_due(now):
                # Woke up outside the window (late, or a weekend), or already done.
                _log(f"{self.name}: slot skipped (outside today's window or already done); "
                     f"next run {self._due}")
                return False
        elif self._retry_at is not None and now >= self._retry_at:
            self._retry_at = None
            if not self._catch_up_due(now):  # past the cutoff, or done
                return False
        else:
            return False
        return self._run(now)

    def run_forever(self) -> None:
        try:
            self.catch_up()
        except Exception:
            logger.exception("scheduler catch-up failed")
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed")

    def stop(self) -> None:
        self._stop.set()


def start_scheduler(func: Callable[[dict], object], path=None) -> Optional[Scheduler]:
    """Start the scheduler thread for ``func``; call it once per process.

    Returns None when there is no schedule file. A bad schedule file raises ValueError
    here, at startup, rather than failing at 06:30.
    """
    schedule = load_schedule(path)
    if schedule is None:
        return None
    scheduler = Scheduler(func, schedule)
    threading.Thread(target=scheduler.run_forever, name="blink-scheduler", daemon=True).start()
    message = f"scheduled {scheduler.name} daily at {schedule.run_time.strftime('%H:%M')} {schedule.tz}"
    logger.info(message)
    _log(message)  # the server only shows warnings by default; this is the proof it started
    return scheduler
