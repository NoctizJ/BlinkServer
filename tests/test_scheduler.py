#!/usr/bin/env python3
"""Tests for the daily scheduler (jobs/scheduler.py).

The clock is injected, the job is mocked, and the state file lives in a temp
dir, so nothing waits, runs a real job, or touches the repo:

    python3 tests/test_scheduler.py
"""

import datetime
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.scheduler as sched

UTC = datetime.timezone.utc
PT = ZoneInfo("America/Los_Angeles")
JOB_NAME = "qqqholdings_job"


def utc(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


def make_schedule(**overrides):
    raw = {"time": "06:30", "timezone": "America/Los_Angeles",
           "weekdays_only": True, "catch_up_until": "12:00"}
    raw.update(overrides)
    return sched.Schedule.from_config(raw)


def _raises(exc_type, fn, *args, label="", **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__} ({label})")


def test_next_run_is_today_if_still_ahead():
    print("Testing next run later today...")
    now = utc(2026, 11, 2, 13, 0)  # Monday 05:00 PST
    nxt = sched.next_run_after(now, datetime.time(6, 30), PT)
    assert nxt.astimezone(UTC) == utc(2026, 11, 2, 14, 30), nxt
    print("  OK")


def test_next_run_is_tomorrow_once_the_time_has_passed():
    print("Testing next run after the time...")
    for now in (utc(2026, 11, 2, 14, 30), utc(2026, 11, 2, 20, 0)):  # 06:30 sharp, 12:00
        nxt = sched.next_run_after(now, datetime.time(6, 30), PT)
        assert nxt.astimezone(UTC) == utc(2026, 11, 3, 14, 30), (now, nxt)
    print("  OK")


def test_next_run_skips_the_weekend_and_follows_dst():
    print("Testing weekend skip across the DST change...")
    now = utc(2026, 10, 30, 14, 0)  # Friday 07:00 PDT, after 06:30
    nxt = sched.next_run_after(now, datetime.time(6, 30), PT)
    # Monday 06:30 is PST (UTC-8) because DST ended on Sunday 2026-11-01.
    assert nxt.astimezone(UTC) == utc(2026, 11, 2, 14, 30), nxt
    anyday = sched.next_run_after(now, datetime.time(6, 30), PT, weekdays_only=False)
    assert anyday.astimezone(UTC) == utc(2026, 10, 31, 13, 30), anyday  # Saturday, PDT
    print("  OK")


def test_from_config_validates_and_applies_defaults():
    print("Testing schedule config parsing...")
    base = {"time": "06:30", "timezone": "America/Los_Angeles"}
    s = sched.Schedule.from_config(base)
    assert s.weekdays_only is True and s.catch_up_until is None, s
    for bad in ({"time": "6.30"}, {"timezone": "Nowhere/Land"}, {"catch_up_until": "noon"}):
        raw = {**base, **bad}
        try:
            sched.Schedule.from_config(raw)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
    print("  OK")


class scheduler_env:
    """A Scheduler with a movable clock, a temp state file and a mocked job."""

    def __init__(self, start, result=None, enabled=True, **schedule_overrides):
        self.now = start
        self.result = {"status": "ok"} if result is None else result
        self.enabled = enabled
        self.schedule_overrides = schedule_overrides
        self.calls = []

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "scheduler.json"

        def job(payload):
            self.calls.append(payload)
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

        self._patches = [
            mock.patch.object(sched, "master_enabled", side_effect=lambda name: self.enabled),
            mock.patch.object(sched, "write_log"),
        ]
        for patch in self._patches:
            patch.start()
        self.scheduler = sched.Scheduler(job, make_schedule(**self.schedule_overrides), name=JOB_NAME,
                                         clock=lambda: self.now, state_path=self.state_path)
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False

    def last_runs(self):
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}


def test_tick_fires_once_when_due():
    print("Testing tick...")
    with scheduler_env(utc(2026, 11, 2, 13, 0)) as env:  # Monday 05:00 PST
        env.now = utc(2026, 11, 2, 13, 30)
        assert env.scheduler.tick() is False and env.calls == []
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is True and len(env.calls) == 1
        env.now = utc(2026, 11, 2, 14, 32)
        assert env.scheduler.tick() is False and len(env.calls) == 1  # next due is tomorrow
        assert env.last_runs() == {JOB_NAME: "2026-11-02"}, env.last_runs()
        env.now = utc(2026, 11, 3, 14, 31)
        assert env.scheduler.tick() is True and len(env.calls) == 2
    print("  OK")


def test_tick_skips_a_disabled_job():
    print("Testing disabled job...")
    with scheduler_env(utc(2026, 11, 2, 13, 0), enabled=False) as env:
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is False and env.calls == []
        assert env.last_runs() == {}
    print("  OK")


def test_a_job_that_raises_or_reports_an_error_is_not_recorded():
    print("Testing failing job...")
    for result in (RuntimeError("boom"), SystemExit(1),
                   {"status": "error", "message": "down"}):
        with scheduler_env(utc(2026, 11, 2, 13, 0), result=result) as env:
            env.now = utc(2026, 11, 2, 14, 31)
            assert env.scheduler.tick() is False, result  # must not raise out of the thread
            assert len(env.calls) == 1 and env.last_runs() == {}, result
            # Not retried before RETRY_AFTER (15 min) has passed.
            env.now = utc(2026, 11, 2, 14, 32)
            assert env.scheduler.tick() is False and len(env.calls) == 1, result
    print("  OK")


def test_a_no_new_data_run_counts_as_done():
    print("Testing no_new_data...")
    with scheduler_env(utc(2026, 11, 2, 13, 0), result={"status": "no_new_data"}) as env:
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is True
        assert env.last_runs() == {JOB_NAME: "2026-11-02"}
    print("  OK")


def test_tick_moves_on_even_if_recording_the_run_fails():
    """A full disk must not make the scheduler re-run the job every 30 seconds."""
    print("Testing a failed state write...")
    with scheduler_env(utc(2026, 11, 2, 13, 0)) as env:
        with mock.patch.object(env.scheduler, "_record_run", side_effect=OSError("disk full")):
            env.now = utc(2026, 11, 2, 14, 31)
            assert env.scheduler.tick() is True
            env.now = utc(2026, 11, 2, 14, 32)
            assert env.scheduler.tick() is False and len(env.calls) == 1
    print("  OK")


def test_a_logging_failure_does_not_break_the_scheduler():
    print("Testing a logging failure...")
    with scheduler_env(utc(2026, 11, 2, 13, 0)) as env:
        sched.write_log.side_effect = RuntimeError("logs/ is read-only")
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is True and len(env.calls) == 1
        env.now = utc(2026, 11, 2, 14, 32)
        assert env.scheduler.tick() is False and len(env.calls) == 1
    print("  OK")


def test_catch_up_runs_a_missed_morning_once():
    print("Testing catch-up...")
    # Server starts Monday 08:00 PST (16:00 UTC), after 06:30, before 12:00.
    with scheduler_env(utc(2026, 11, 2, 16, 0)) as env:
        env.scheduler.catch_up()
        assert len(env.calls) == 1 and env.last_runs() == {JOB_NAME: "2026-11-02"}
        env.scheduler.catch_up()  # a second start the same day does nothing
        assert len(env.calls) == 1
    print("  OK")


def test_catch_up_does_not_fire_outside_its_window():
    print("Testing catch-up window...")
    cases = {
        "before 06:30": utc(2026, 11, 2, 13, 0),
        "after the 12:00 cutoff": utc(2026, 11, 2, 21, 0),
        "on a Saturday": utc(2026, 10, 31, 15, 0),
    }
    for label, start in cases.items():
        with scheduler_env(start) as env:
            env.scheduler.catch_up()
            assert env.calls == [], label
    with scheduler_env(utc(2026, 11, 2, 16, 0), catch_up_until=None) as env:
        env.scheduler.catch_up()
        assert env.calls == [], "no catch_up_until configured"
    print("  OK")


def test_schedule_config_file_is_valid():
    print("Testing configs/schedule.json...")
    s = sched.load_schedule()
    assert s.run_time == datetime.time(6, 30) and s.tz == PT, s
    assert s.weekdays_only is True and s.catch_up_until == datetime.time(12, 0), s
    print("  OK")


def test_a_failed_run_is_retried_later_the_same_morning():
    print("Testing retry after a failure...")
    with scheduler_env(utc(2026, 11, 2, 13, 0), result={"status": "error", "message": "down"}) as env:
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is False and len(env.calls) == 1
        env.now = utc(2026, 11, 2, 14, 40)
        assert env.scheduler.tick() is False and len(env.calls) == 1        # too soon
        env.now = utc(2026, 11, 2, 14, 46)
        assert env.scheduler.tick() is False and len(env.calls) == 2        # retry, fails again
        env.result = {"status": "ok"}                                   # Invesco is back
        env.now = utc(2026, 11, 2, 15, 1)
        assert env.scheduler.tick() is True and len(env.calls) == 3
        assert env.last_runs() == {JOB_NAME: "2026-11-02"}
        env.now = utc(2026, 11, 2, 15, 30)
        assert env.scheduler.tick() is False and len(env.calls) == 3        # done: no more retries
    print("  OK")


def test_retries_stop_when_the_window_closes():
    print("Testing the end of the retry window...")
    with scheduler_env(utc(2026, 11, 2, 13, 0), result=RuntimeError("down")) as env:
        env.now = utc(2026, 11, 2, 14, 31)
        end = utc(2026, 11, 2, 21, 0)                                   # 13:00 PST, past the noon cutoff
        while env.now < end:
            env.scheduler.tick()
            env.now += datetime.timedelta(seconds=30)
        # 14:31 and then every 15 minutes up to 19:46 UTC (11:46 PST); 20:01 is past noon.
        assert len(env.calls) == 22, len(env.calls)
        assert env.last_runs() == {}
    print("  OK")


def test_a_switched_off_job_is_skipped_for_the_day_not_retried():
    print("Testing a switched-off job...")
    with scheduler_env(utc(2026, 11, 2, 13, 0), enabled=False) as env:
        env.now = utc(2026, 11, 2, 14, 31)
        assert env.scheduler.tick() is False
        env.enabled = True
        env.now = utc(2026, 11, 2, 15, 30)
        assert env.scheduler.tick() is False and env.calls == []
        assert env.scheduler._retry_at is None
    print("  OK")


def test_a_failed_startup_catch_up_is_retried():
    print("Testing a failed catch-up...")
    with scheduler_env(utc(2026, 11, 2, 16, 0), result={"status": "error", "message": "down"}) as env:
        env.scheduler.catch_up()
        assert len(env.calls) == 1
        env.now = utc(2026, 11, 2, 16, 16)
        env.scheduler.tick()
        assert len(env.calls) == 2
    print("  OK")


def test_waking_up_outside_the_window_skips_the_missed_slot():
    """A Mac that slept past 06:30 must not run the job on a Saturday, or late at night."""
    print("Testing waking from sleep outside the window...")
    with scheduler_env(utc(2026, 10, 30, 12, 0)) as env:                # Friday 05:00 PDT
        env.now = utc(2026, 10, 31, 17, 0)                              # woke Saturday 10:00 PDT
        assert env.scheduler.tick() is False and env.calls == []
        env.now = utc(2026, 11, 2, 14, 0)                               # Monday 06:00 PST
        assert env.scheduler.tick() is False and env.calls == []
        env.now = utc(2026, 11, 2, 14, 30)                              # Monday 06:30 PST: the real slot
        assert env.scheduler.tick() is True and len(env.calls) == 1
    with scheduler_env(utc(2026, 10, 29, 12, 0)) as env:                # Thursday 05:00 PDT
        env.now = utc(2026, 10, 31, 6, 0)                               # woke Friday 23:00 PDT
        assert env.scheduler.tick() is False and env.calls == []
    print("  OK")


def test_waking_up_inside_the_window_runs_the_missed_slot_once():
    print("Testing waking from sleep inside the window...")
    with scheduler_env(utc(2026, 10, 30, 12, 0)) as env:                # Friday 05:00 PDT
        env.now = utc(2026, 10, 30, 16, 0)                              # woke Friday 09:00 PDT
        assert env.scheduler.tick() is True and len(env.calls) == 1
        env.now = utc(2026, 10, 30, 16, 30)
        assert env.scheduler.tick() is False and len(env.calls) == 1
    print("  OK")


def test_starting_exactly_at_the_run_time_runs_once():
    print("Testing a start at 06:30:00 sharp...")
    with scheduler_env(utc(2026, 11, 2, 14, 30)) as env:
        env.scheduler.catch_up()
        assert len(env.calls) == 1
        for _ in range(40):                                             # 20 minutes of ticks
            env.now += datetime.timedelta(seconds=30)
            env.scheduler.tick()
        assert len(env.calls) == 1
    print("  OK")


def test_next_run_in_the_repeated_hour_is_never_in_the_past():
    """01:30 happens twice on 2026-11-01. Started in the second pass (01:20 PST), that day's
    slot (the first 01:30, PDT) is already past, so the next run is tomorrow's, not a past time."""
    print("Testing the repeated hour...")
    now = utc(2026, 11, 1, 9, 20)
    nxt = sched.next_run_after(now, datetime.time(1, 30), PT, weekdays_only=False)
    assert nxt > now, nxt
    assert nxt.astimezone(UTC) == utc(2026, 11, 2, 9, 30), nxt
    print("  OK")


def test_a_damaged_state_file_is_treated_as_empty():
    print("Testing a damaged state file...")
    for damage in (b"[1]", b"null", b"{oops", b"\xff\xfe"):
        with scheduler_env(utc(2026, 11, 2, 16, 0)) as env:
            env.state_path.write_bytes(damage)
            env.scheduler.catch_up()
            assert len(env.calls) == 1, damage
            assert env.last_runs() == {JOB_NAME: "2026-11-02"}, damage
    print("  OK")


def test_from_config_rejects_bad_entries():
    print("Testing schedule validation...")
    good = {"time": "06:30", "timezone": "America/Los_Angeles"}
    bad_entries = [
        {k: v for k, v in good.items() if k != "time"},
        {k: v for k, v in good.items() if k != "timezone"},
        {**good, "weekdays_only": "false"},         # bool("false") is True: refuse, don't guess
        {**good, "catch_up_untill": "12:00"},       # a typo must not silently disable catch-up
        "not an object",
    ]
    for entry in bad_entries:
        _raises(ValueError, sched.Schedule.from_config, entry)
    print("  OK")


def test_load_schedule_handles_missing_and_bad_files():
    print("Testing schedule file validation...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedule.json"
        assert sched.load_schedule(Path(tmp) / "missing.json") is None
        for label, content in {"a list": [1], "a bad time": {"time": "25:99", "timezone": "America/Los_Angeles"}}.items():
            path.write_text(json.dumps(content))
            _raises(ValueError, sched.load_schedule, path, label=label)
    print("  OK")


def test_start_scheduler_starts_one_daemon_thread():
    """Uses a fake Thread, so no real thread is ever started."""
    print("Testing start_scheduler...")
    started = []

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            started.append((target, name, daemon))

        def start(self):
            pass

    def good_job(payload):
        return {"status": "ok"}

    good = {"time": "06:30", "timezone": "America/Los_Angeles"}
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(sched.threading, "Thread", FakeThread), \
            mock.patch.object(sched, "write_log") as log:
        path = Path(tmp) / "schedule.json"
        path.write_text(json.dumps(good))
        scheduler = sched.start_scheduler(good_job, path)
        assert isinstance(scheduler, sched.Scheduler)
        assert [(s[1], s[2]) for s in started] == [("blink-scheduler", True)], started
        # The server only shows warnings by default, so the start must reach the log.
        assert any("06:30" in str(c) for c in log.call_args_list), log.call_args_list
        assert sched.start_scheduler(good_job, Path(tmp) / "missing.json") is None
        path.write_text(json.dumps({**good, "time": "25:99"}))
        _raises(ValueError, sched.start_scheduler, good_job, path)
        assert len(started) == 1
    print("  OK")


def test_run_forever_survives_a_failing_catch_up_and_tick():
    print("Testing run_forever resilience...")
    with scheduler_env(utc(2026, 11, 2, 13, 0)) as env:
        env.scheduler.stop()                                            # the loop ends right after catch-up
        with mock.patch.object(env.scheduler, "catch_up", side_effect=RuntimeError("boom")):
            env.scheduler.run_forever()                                 # must return, not raise
    with scheduler_env(utc(2026, 11, 2, 13, 0)) as env:
        def boom():
            env.scheduler.stop()
            raise RuntimeError("boom")
        with mock.patch.object(sched, "TICK_SECONDS", 0), \
                mock.patch.object(env.scheduler, "tick", side_effect=boom):
            env.scheduler.run_forever()
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} scheduler tests passed!")
