#!/usr/bin/env python3
"""Tests for the OHLCV repair tool (jobs/ticker_ohlcv_repair.py).

Nothing touches the network or the repo: every download is faked (keyed by symbol,
and honouring the requested date range like Yahoo does), stock/ohlcv is redirected to
a temp dir, and the logging engine is mocked. Needs pandas (skips itself without it):

    python3 tests/test_ticker_ohlcv_repair.py
"""

import contextlib
import datetime
import io
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock

try:
    import pandas as pd
except ImportError:
    print("pandas is not installed; skipping the OHLCV repair tests")
    sys.exit(0)

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import jobs.ticker_ohlcv as tk
import jobs.ticker_ohlcv_repair as repair
import jobs.ticker_ohlcv_store as store
import ohlcv_helpers as helpers
from ohlcv_helpers import holding, raises, temp_qqq

D = datetime.date
DAYS = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]   # Monday .. Friday
RANGE = "2026-11-09..2026-11-13"
START, END = D(2026, 11, 9), D(2026, 11, 13)
NY = ZoneInfo("America/New_York")
AFTER_CLOSE = datetime.datetime(2026, 11, 13, 17, 30, tzinfo=NY)   # Friday: the 13th's bar is final
_real_last_final_day = repair.last_final_day


class temp_ohlcv(helpers.temp_ohlcv):
    """The shared temp store with both loggers mocked (``log`` is the repair one), a ``path`` and ``dates``
    helper, and the clock set to Friday 17:30 New York (unless a test passes its own ``now``), so no test
    depends on the time it runs."""

    def __init__(self):
        super().__init__(tk, repair)

    def __enter__(self):
        super().__enter__()
        self._clock = mock.patch.object(repair, "last_final_day",
                                        lambda now=None: _real_last_final_day(now or AFTER_CLOSE))
        self._clock.start()
        return self

    def __exit__(self, *exc):
        self._clock.stop()
        return super().__exit__(*exc)

    def path(self, symbol):
        return self.dir / f"{symbol}.json"

    def dates(self, symbol):
        return [r["date"] for r in json.loads(self.path(symbol).read_text())]


def bar(date, value=1.0):
    return {"date": date, "open": value, "high": value + 1, "low": value - 1, "close": value, "volume": 10}


def save(symbol, dates, value=1.0):
    """Put a ticker's file on disk with a bar for each of ``dates``."""
    store.save_merged(symbol, [bar(d, value) for d in dates])


def frame(dates):
    index = pd.DatetimeIndex(dates, tz="America/New_York")
    n = len(dates)
    return pd.DataFrame({"Open": [5.0] * n, "High": [6.0] * n, "Low": [4.0] * n, "Close": [5.5] * n,
                         "Adj Close": [5.5] * n, "Volume": [99] * n,
                         "Dividends": [0.0] * n, "Stock Splits": [0.0] * n}, index=index)


def fake_download(table):
    """A _download replacement. table maps symbol -> the dates Yahoo has for it, or an exception.

    Like Yahoo, it only returns the days inside the requested start..end."""
    calls = []

    def download(symbol, start, end):
        calls.append((symbol, start, end))
        value = table[symbol]
        if isinstance(value, Exception):
            raise value
        inside = [d for d in value if start.isoformat() <= d <= end.isoformat()]
        return frame(inside) if inside else pd.DataFrame()

    download.calls = calls
    return download


def patched(table):
    dl = fake_download(table)
    return dl, mock.patch.object(tk, "_download", dl)


# ---------------------------------------------------------------- trading_days

def test_trading_days_are_the_reference_funds_dates():
    print("Testing trading_days...")
    holiday_gap = ["2026-11-09", "2026-11-10", "2026-11-12", "2026-11-13"]
    dl, patch = patched({"QQQ": holiday_gap})
    with temp_ohlcv(), patch:
        assert repair.trading_days(START, END) == holiday_gap
    assert dl.calls == [("QQQ", START, END)], dl.calls
    assert repair.REFERENCE_SYMBOL == "QQQ"
    print("  OK")


def test_trading_days_with_no_bars_is_an_error():
    print("Testing trading_days empty...")
    dl, patch = patched({"QQQ": []})
    with temp_ohlcv(), patch:
        exc = raises(ValueError, repair.trading_days, START, END)
        assert "QQQ has no bars" in str(exc), exc
    print("  OK")


# ------------------------------------------------------------------ find_holes

def test_find_holes():
    print("Testing find_holes...")
    with temp_ohlcv():
        save("AAPL", ["2026-11-09", "2026-11-11", "2026-11-13", "2026-11-20"])   # an extra day is fine
        assert repair.find_holes("AAPL", DAYS) == ["2026-11-10", "2026-11-12"]
        save("DONE", DAYS)
        assert repair.find_holes("DONE", DAYS) == []
        assert repair.find_holes("NOFILE", DAYS) == DAYS
        assert repair.find_holes("AAPL", []) == []
    print("  OK")


# --------------------------------------------------------------- repair_ticker

def test_repair_ticker_complete():
    print("Testing repair_ticker complete...")
    dl, patch = patched({})
    with temp_ohlcv() as t, patch:
        save("AAPL", DAYS)
        before = t.path("AAPL").read_text()
        assert repair.repair_ticker("AAPL", DAYS, True) == {"ticker": "AAPL", "status": "complete"}
        assert dl.calls == [] and t.path("AAPL").read_text() == before
    print("  OK")


def test_repair_ticker_fills_holes_and_merges_into_the_file():
    print("Testing repair_ticker fill...")
    dl, patch = patched({"AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        save("AAPL", ["2026-11-09", "2026-11-10", "2026-11-13"])
        res = repair.repair_ticker("AAPL", DAYS, True)
        assert res == {"ticker": "AAPL", "status": "holes", "missing": ["2026-11-11", "2026-11-12"],
                       "filled": ["2026-11-11", "2026-11-12"], "still_missing": []}, res
        assert dl.calls == [("AAPL", D(2026, 11, 11), D(2026, 11, 12))], dl.calls   # first..last hole
        saved = json.loads(t.path("AAPL").read_text())
        assert [r["date"] for r in saved] == DAYS, saved
        assert saved[0] == bar("2026-11-09") and saved[4] == bar("2026-11-13"), saved   # old bars kept
        assert saved[2]["close"] == 5.5, saved                                           # new bars added
    print("  OK")


def test_repair_ticker_reports_what_yahoo_could_not_supply():
    print("Testing repair_ticker partly filled...")
    dl, patch = patched({"NEW": ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]})  # no bar for 11-09
    with temp_ohlcv() as t, patch:
        save("NEW", ["2026-11-12"])
        res = repair.repair_ticker("NEW", DAYS, True)
        assert res["status"] == "holes", res
        assert res["missing"] == ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-13"], res
        assert res["filled"] == ["2026-11-10", "2026-11-11", "2026-11-13"], res
        assert res["still_missing"] == ["2026-11-09"], res
        assert dl.calls == [("NEW", START, END)], dl.calls
        assert t.dates("NEW") == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    print("  OK")


def test_repair_ticker_fetch_failure_keeps_the_file_and_reports_the_reason():
    print("Testing repair_ticker fetch failure...")
    dl, patch = patched({"AAPL": ConnectionError("down")})
    with temp_ohlcv() as t, patch:
        save("AAPL", ["2026-11-09", "2026-11-13"])
        before = t.path("AAPL").read_text()
        res = repair.repair_ticker("AAPL", DAYS, True)
        assert res == {"ticker": "AAPL", "status": "holes",
                       "missing": ["2026-11-10", "2026-11-11", "2026-11-12"], "filled": [],
                       "still_missing": ["2026-11-10", "2026-11-11", "2026-11-12"],
                       "reason": "ConnectionError: down"}, res
        assert t.path("AAPL").read_text() == before
    print("  OK")


def test_repair_ticker_damaged_file_fails_and_is_left_alone():
    print("Testing repair_ticker damaged file...")
    dl, patch = patched({"AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        t.dir.mkdir(parents=True)
        t.path("AAPL").write_text("{oops", encoding="utf-8")
        res = repair.repair_ticker("AAPL", DAYS, True)
        assert res["status"] == "failed" and res["reason"].startswith("JSONDecodeError:"), res
        assert t.path("AAPL").read_text() == "{oops" and dl.calls == []
    print("  OK")


def test_repair_ticker_invalid_symbol_fails_without_touching_anything():
    print("Testing repair_ticker invalid symbol...")
    dl, patch = patched({})
    with temp_ohlcv() as t, patch:
        for symbol in ("../x", "A/B", ".hidden", ""):
            res = repair.repair_ticker(symbol, DAYS, True)
            assert res["status"] == "failed" and "not a valid ticker symbol" in res["reason"], res
        assert dl.calls == [] and t.everything() == [], t.everything()
    print("  OK")


def test_check_only_never_downloads_for_the_ticker():
    print("Testing check only...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        save("AAPL", ["2026-11-09"])
        before = t.path("AAPL").read_text()
        res = repair.run({"range": RANGE, "tickers": "AAPL", "fill": False})
        assert res["status"] == "partial", res
        r = res["results"][0]
        assert r["missing"] == DAYS[1:] and r["filled"] == [] and r["still_missing"] == DAYS[1:], r
        assert dl.calls == [("QQQ", START, END)], dl.calls    # the reference is the only request
        assert t.path("AAPL").read_text() == before
        assert "-> not filled (check only)" in res["message"], res["message"]
    print("  OK")


def test_a_ticker_with_no_file_is_backfilled_over_the_span_of_the_trading_days():
    print("Testing backfill...")
    dl, patch = patched({"QQQ": DAYS, "NEWCO": DAYS})
    with temp_ohlcv() as t, patch:
        res = repair.run({"range": RANGE, "tickers": "NEWCO"})
        assert res["status"] == "ok", res
        assert dl.calls == [("QQQ", START, END), ("NEWCO", START, END)], dl.calls
        assert t.dates("NEWCO") == DAYS
        assert res["results"][0]["filled"] == DAYS and res["results"][0]["still_missing"] == []
    print("  OK")


def test_the_fetch_span_is_first_to_last_missing_day():
    print("Testing fetch span...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv(), patch:
        save("AAPL", ["2026-11-10", "2026-11-11"])    # holes: 11-09, 11-12, 11-13
        repair.run({"range": RANGE, "tickers": "AAPL"})
        assert dl.calls[1] == ("AAPL", D(2026, 11, 9), D(2026, 11, 13)), dl.calls
    print("  OK")


# ------------------------------------------------------------------------- run

def test_run_all_complete():
    print("Testing run all complete...")
    dl, patch = patched({"QQQ": DAYS})
    with temp_ohlcv() as t, patch:
        save("AAPL", DAYS)
        save("MSFT", DAYS)
        res = repair.run({"range": RANGE, "tickers": "AAPL,MSFT"})
        assert res["status"] == "ok", res
        assert res["message"] == ("Checked 2 tickers over 5 trading days (2026-11-09 .. 2026-11-13): "
                                  "2 complete"), res["message"]
        assert res["trading_days"] == DAYS and res["start"] == "2026-11-09" and res["end"] == "2026-11-13"
        assert [r["status"] for r in res["results"]] == ["complete", "complete"]
        t.log.assert_called_once_with("ticker_ohlcv_repair", res["message"])
        assert dl.calls == [("QQQ", START, END)], dl.calls
    print("  OK")


def test_run_mixed_results_message():
    print("Testing run mixed...")
    table = {"QQQ": DAYS, "FILL": DAYS, "STILL": ["2026-11-12"]}
    dl, patch = patched(table)
    with temp_ohlcv() as t, patch:
        save("COMP", DAYS)
        save("FILL", ["2026-11-09", "2026-11-10", "2026-11-13"])
        save("STILL", ["2026-11-09", "2026-11-10", "2026-11-11"])
        t.path("BAD").write_text("[1]", encoding="utf-8")
        res = repair.run({"range": RANGE, "tickers": "COMP,FILL,STILL,BAD"})
        assert res["status"] == "partial", res
        assert res["message"].splitlines() == [
            "FILL     2 missing (2026-11-11, 2026-11-12) -> filled 2",
            "STILL    2 missing (2026-11-12, 2026-11-13) -> filled 1, still missing 1 (2026-11-13)",
            "BAD      FAILED  ValueError: BAD.json is not a list of daily records",
            "Checked 4 tickers over 5 trading days (2026-11-09 .. 2026-11-13): 1 complete, "
            "2 with holes (4 missing, 3 filled, 1 still missing), 1 could not be checked",
        ], res["message"].splitlines()
        assert [r["ticker"] for r in res["results"]] == ["COMP", "FILL", "STILL", "BAD"]
        t.log.assert_called_once_with("ticker_ohlcv_repair", res["message"].splitlines()[-1])
    print("  OK")


def test_run_everything_filled_is_ok():
    print("Testing run all filled...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv(), patch:
        save("AAPL", ["2026-11-09"])
        res = repair.run({"range": RANGE, "tickers": "AAPL"})
        assert res["status"] == "ok", res
        assert res["message"].splitlines() == [
            "AAPL     4 missing (2026-11-10, 2026-11-11, 2026-11-12, 2026-11-13) -> filled 4",
            "Checked 1 tickers over 5 trading days (2026-11-09 .. 2026-11-13): 0 complete, "
            "1 with holes (4 missing, 4 filled, 0 still missing)"], res["message"]
    print("  OK")


def test_run_a_failed_fetch_is_shown_on_the_ticker_line():
    print("Testing run fetch failed line...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": ConnectionError("down")})
    with temp_ohlcv(), patch:
        res = repair.run({"range": RANGE, "tickers": "AAPL"})
        assert res["status"] == "partial", res
        assert res["message"].splitlines()[0] == (
            "AAPL     5 missing (2026-11-09, 2026-11-10, 2026-11-11, 2026-11-12, 2026-11-13) "
            "-> still missing 5 (2026-11-09, 2026-11-10, 2026-11-11, 2026-11-12, 2026-11-13); "
            "fetch failed: ConnectionError: down"), res["message"]
    print("  OK")


def test_run_reference_failure_is_an_error():
    print("Testing run reference failure...")
    for ref in (ConnectionError("down"), []):
        dl, patch = patched({"QQQ": ref, "AAPL": DAYS})
        with temp_ohlcv() as t, patch:
            res = repair.run({"range": RANGE, "tickers": "AAPL"})
            assert res["status"] == "error", res
            assert "could not get the trading days from QQQ" in res["message"], res
            assert dl.calls == [("QQQ", START, END)] and t.everything() == [], dl.calls
    print("  OK")


def test_run_bad_arguments_never_download():
    print("Testing run bad arguments...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        for payload in ({}, None, {"tickers": "AAPL"}, {"range": RANGE}, {"range": "xx", "tickers": "AAPL"},
                        {"range": RANGE, "tickers": ""}, {"range": RANGE, "tickers": []},
                        {"range": RANGE, "tickers": "AAPL", "fill": "maybe"},
                        {"range": RANGE, "tickers": "AAPL", "fill": None},
                        {"range": "2026-12-31..", "tickers": "AAPL"}):
            res = repair.run(payload)
            assert res["status"] == "error" and res["message"], (payload, res)
        assert dl.calls == [], dl.calls
        t.log.assert_not_called()
    print("  OK")


def test_fill_accepts_json_and_query_string_values():
    print("Testing fill values...")
    for value, fills in ((False, False), ("false", False), ("0", False), ("False", False), ("off", False),
                         (True, True), ("true", True), ("1", True), ("TRUE", True), ("yes", True)):
        dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
        with temp_ohlcv() as t, patch:
            repair.run({"range": RANGE, "tickers": "AAPL", "fill": value})
            assert (len(dl.calls) == 2) is fills, (value, dl.calls)
            assert t.path("AAPL").exists() is fills, value
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv(), patch:
        repair.run({"range": RANGE, "tickers": "AAPL"})
        assert len(dl.calls) == 2, dl.calls    # fill defaults to true
    print("  OK")


def test_run_current_qqq():
    print("Testing run CurrentQQQ...")
    dl, patch = patched({"QQQ": DAYS, "NVDA": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch, temp_qqq({"AAPL": holding("Apple", 5.0), "NVDA": holding("Nvidia", 9.0)}):
        res = repair.run({"range": RANGE, "tickers": "CurrentQQQ"})
        assert res["status"] == "ok", res
        assert [r["ticker"] for r in res["results"]] == ["NVDA", "AAPL"], res
        assert [c[0] for c in dl.calls] == ["QQQ", "NVDA", "AAPL"], dl.calls
    print("  OK")


def test_long_date_lists_are_cut_in_the_message_but_complete_in_the_results():
    print("Testing long date lists...")
    days = [f"2026-11-{n:02d}" for n in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
    dl, patch = patched({"QQQ": days})
    with temp_ohlcv(), patch:
        res = repair.run({"range": "2026-11-02..2026-11-13", "tickers": "AAPL", "fill": False})
        line = res["message"].splitlines()[0]
        assert line == ("AAPL     10 missing (2026-11-02, 2026-11-03, 2026-11-04, 2026-11-05, 2026-11-06 "
                        "(+5 more)) -> not filled (check only)"), line
        assert res["results"][0]["missing"] == days, res["results"][0]
        assert repair._dates_text(days[:5]) == "2026-11-02, 2026-11-03, 2026-11-04, 2026-11-05, 2026-11-06"
    print("  OK")


def test_run_survives_a_logging_failure():
    print("Testing run with failing log...")
    dl, patch = patched({"QQQ": DAYS})
    with temp_ohlcv() as t, patch:
        save("AAPL", DAYS)
        t.log.side_effect = RuntimeError("log broken")
        assert repair.run({"range": RANGE, "tickers": "AAPL"})["status"] == "ok"
    print("  OK")


# ------------------------------------------------------- today's partial bar

def test_last_final_day():
    print("Testing last_final_day...")
    LA, UTC = ZoneInfo("America/Los_Angeles"), ZoneInfo("UTC")
    cases = [
        (datetime.datetime(2026, 11, 13, 10, 0, tzinfo=NY), D(2026, 11, 12)),     # morning: yesterday
        (datetime.datetime(2026, 11, 13, 16, 59, tzinfo=NY), D(2026, 11, 12)),    # just before 17:00
        (datetime.datetime(2026, 11, 13, 17, 0, tzinfo=NY), D(2026, 11, 13)),     # 17:00 sharp: final
        (datetime.datetime(2026, 11, 13, 23, 59, tzinfo=NY), D(2026, 11, 13)),
        (datetime.datetime(2026, 11, 14, 0, 0, tzinfo=NY), D(2026, 11, 13)),      # just after midnight
        (datetime.datetime(2026, 11, 13, 13, 59, tzinfo=LA), D(2026, 11, 12)),    # 16:59 in New York
        (datetime.datetime(2026, 11, 13, 14, 0, tzinfo=LA), D(2026, 11, 13)),     # 17:00 in New York
        (datetime.datetime(2026, 11, 13, 21, 30, tzinfo=UTC), D(2026, 11, 12)),   # 16:30 in New York
    ]
    for now, want in cases:
        assert _real_last_final_day(now) == want, (now, _real_last_final_day(now), want)
    # New York time, so the answer is the same on either side of the daylight-saving change.
    assert _real_last_final_day(datetime.datetime(2026, 7, 1, 16, 59, tzinfo=NY)) == D(2026, 6, 30)
    assert _real_last_final_day(datetime.datetime(2026, 7, 1, 17, 0, tzinfo=NY)) == D(2026, 7, 1)
    assert _real_last_final_day(datetime.datetime(2026, 12, 1, 16, 59, tzinfo=NY)) == D(2026, 11, 30)
    assert _real_last_final_day(datetime.datetime(2026, 12, 1, 17, 0, tzinfo=NY)) == D(2026, 12, 1)
    assert isinstance(_real_last_final_day(), datetime.date)
    print("  OK")


MORNING = datetime.datetime(2026, 11, 13, 10, 0, tzinfo=NY)   # Friday 10:00 New York: Friday's bar is not final


def test_before_17_00_new_york_todays_partial_bar_is_not_asked_for_or_saved():
    print("Testing the clamp before 17:00 New York...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        res = repair.run({"range": RANGE, "tickers": "AAPL"}, now=MORNING)
        assert res["status"] == "ok", res
        assert res["end"] == "2026-11-12" and res["trading_days"] == DAYS[:-1], res
        assert dl.calls == [("QQQ", START, D(2026, 11, 12)), ("AAPL", START, D(2026, 11, 12))], dl.calls
        assert t.dates("AAPL") == DAYS[:-1]
        assert res["message"].splitlines()[-1] == ("(today's bar is not final before 17:00 New York time, "
                                                   "so the check stopped at 2026-11-12)"), res["message"]
        t.log.assert_called_once_with("ticker_ohlcv_repair", res["message"].splitlines()[-2])
    print("  OK")


def test_after_17_00_new_york_today_is_included():
    print("Testing no clamp after 17:00 New York...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        res = repair.run({"range": RANGE, "tickers": "AAPL"}, now=AFTER_CLOSE)
        assert res["end"] == "2026-11-13" and res["trading_days"] == DAYS, res
        assert dl.calls == [("QQQ", START, END), ("AAPL", START, END)], dl.calls
        assert t.dates("AAPL") == DAYS and "not final" not in res["message"], res["message"]
    print("  OK")


def test_a_range_that_is_only_today_before_17_00_has_nothing_to_check_yet():
    print("Testing a range of only today before 17:00...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        for spec in ("2026-11-13..2026-11-13", "2026-11-13.."):
            res = repair.run({"range": spec, "tickers": "AAPL"}, now=MORNING)
            assert res == {"status": "error", "message": "nothing to check yet: the range ends after the last "
                                                         "finished trading day (2026-11-12)"}, res
        assert dl.calls == [] and t.everything() == [], dl.calls
        t.log.assert_not_called()
    print("  OK")


def test_ranges_ending_before_the_last_final_day_are_not_clamped():
    print("Testing ranges that need no clamp...")
    dl, patch = patched({"QQQ": DAYS, "AAPL": DAYS})
    with temp_ohlcv() as t, patch:
        res = repair.run({"range": "2026-11-09..2026-11-11", "tickers": "AAPL"}, now=MORNING)
        assert res["end"] == "2026-11-11" and "not final" not in res["message"], res
        assert dl.calls[0] == ("QQQ", START, D(2026, 11, 11)), dl.calls
        # A weekend: Saturday 10:00 New York, the last final day is Friday, and a range ending then is whole.
        saturday = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=NY)
        res = repair.run({"range": RANGE, "tickers": "AAPL"}, now=saturday)
        assert res["end"] == "2026-11-13" and "not final" not in res["message"], res
    print("  OK")


# ------------------------------------------------------------------------ main

def _main(argv, table):
    out = io.StringIO()
    dl, patch = patched(table)
    with patch, contextlib.redirect_stdout(out):
        code = repair.main(argv)
    return code, out.getvalue(), dl


def test_main_prints_the_message_and_returns_zero_for_ok():
    print("Testing main ok...")
    with temp_ohlcv():
        save("AAPL", DAYS)
        code, out, _ = _main([RANGE, "AAPL"], {"QQQ": DAYS})
        assert code == 0, code
        assert out == "Checked 1 tickers over 5 trading days (2026-11-09 .. 2026-11-13): 1 complete\n", out
    print("  OK")


def test_main_returns_one_when_not_ok():
    print("Testing main not ok...")
    with temp_ohlcv():
        code, out, _ = _main([RANGE, "AAPL", "--check-only"], {"QQQ": DAYS})
        assert code == 1 and "5 missing" in out and "not filled" in out, (code, out)
        code, out, dl = _main(["nonsense", "AAPL"], {})
        assert code == 1 and dl.calls == [] and "range" in out, (code, out)
    print("  OK")


def test_main_check_only_passes_fill_false():
    print("Testing main --check-only...")
    with mock.patch.object(repair, "run", return_value={"status": "ok", "message": "m"}) as run, \
            contextlib.redirect_stdout(io.StringIO()):
        assert repair.main(["30d", "AAPL"]) == 0
        assert repair.main(["30d", "AAPL", "GOOGL", "--check-only"]) == 0
    assert run.call_args_list == [
        mock.call({"range": "30d", "tickers": ["AAPL"], "fill": True}),
        mock.call({"range": "30d", "tickers": ["AAPL", "GOOGL"], "fill": False})], run.call_args_list
    print("  OK")


def test_main_requires_range_and_tickers():
    print("Testing main missing arguments...")
    for argv in ([], ["30d"]):
        with contextlib.redirect_stderr(io.StringIO()):
            exc = raises(SystemExit, repair.main, argv)
        assert exc.code == 2, (argv, exc.code)
    print("  OK")


# ---------------------------------------------------------------- registration

def test_webhook_and_log_switch_are_registered():
    print("Testing registration...")
    config = json.loads((ROOT / "configs" / "config.json").read_text(encoding="utf-8"))
    entries = [w for w in config["webhooks"] if w["path"] == "/webhook/ticker/ohlcv/repair"]
    assert len(entries) == 1, entries
    assert entries[0]["module"] == "jobs.ticker_ohlcv_repair" and entries[0]["function"] == "run", entries
    assert entries[0]["require_secret"] is True, entries
    assert hasattr(repair, "run")
    switches = json.loads((ROOT / "configs" / "log_switches.json").read_text(encoding="utf-8"))
    assert switches["types"]["ticker_ohlcv_repair"] is True, switches
    assert repair.LOG_TYPE == "ticker_ohlcv_repair"
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} OHLCV repair tests passed!")
