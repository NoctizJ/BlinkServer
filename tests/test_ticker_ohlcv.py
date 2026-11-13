#!/usr/bin/env python3
"""Tests for the OHLCV history fetcher (jobs/ticker_ohlcv.py).

Nothing touches the network or the repo: every download is faked with a pandas
DataFrame shaped like yfinance's, stock/ohlcv is redirected to a temp dir, and the
logging engine is mocked. Needs pandas (skips itself without it):

    python3 tests/test_ticker_ohlcv.py
"""

import contextlib
import datetime
import io
import json
import sys
import types
from pathlib import Path
from unittest import mock

try:
    import pandas as pd
except ImportError:
    print("pandas is not installed; skipping the OHLCV tests")
    sys.exit(0)

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import jobs.ticker_ohlcv as tk
import ohlcv_helpers as helpers
from ohlcv_helpers import REC1, holding, raises, rec, temp_qqq, write_snapshot

TODAY = datetime.date(2026, 11, 13)
D = datetime.date


def temp_ohlcv():
    """Point the module at a temp stock/ohlcv dir and mock its logger."""
    return helpers.temp_ohlcv(tk)


def frame(*rows):
    """A yfinance-like history frame from (date, open, high, low, close, volume) rows."""
    index = pd.DatetimeIndex([r[0] for r in rows], tz="America/New_York")
    return pd.DataFrame({
        "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
        "Adj Close": [r[4] for r in rows], "Volume": [r[5] for r in rows],
        "Dividends": [0.0] * len(rows), "Stock Splits": [0.0] * len(rows),
    }, index=index)


DF1 = frame(("2026-11-10", 10.0, 11.0, 9.0, 10.5, 1000), ("2026-11-11", 10.5, 12.0, 10.0, 11.0, 2000))


def fake_download(table):
    """A _download replacement: table maps symbol -> DataFrame, None (empty) or an exception."""
    calls = []

    def download(symbol, start, end):
        calls.append((symbol, start, end))
        value = table[symbol]
        if isinstance(value, Exception):
            raise value
        return value

    download.calls = calls
    return download


# ---------------------------------------------------------------- parse_range

def test_parse_range_days():
    print("Testing parse_range days...")
    want = (D(2026, 10, 14), TODAY)
    for spec in ("30d", "30", " 30D ", 30):
        assert tk.parse_range(spec, TODAY) == want, spec
    print("  OK")


def test_parse_range_dates():
    print("Testing parse_range dates...")
    assert tk.parse_range("2026-01-01..2026-03-31", TODAY) == (D(2026, 1, 1), D(2026, 3, 31))
    assert tk.parse_range("2026-01-01..", TODAY) == (D(2026, 1, 1), TODAY)
    assert tk.parse_range(" 2026-01-01 .. 2026-01-01 ", TODAY) == (D(2026, 1, 1), D(2026, 1, 1))
    print("  OK")


def test_parse_range_rejects_bad_input():
    print("Testing parse_range errors...")
    bad = ["", None, "0d", "abc", "-5", "2026-03-31..2026-01-01", "2026-02-30..2026-03-01",
           "20260101..20260201", "2026-01-01", "2026-11-14..", "2026-11-14..2026-12-01",
           "٢٠٢٦-٠١-٠١..٢٠٢٦-٠٢-٠١", "2026-01-01..tomorrow", "..2026-01-01"]
    for spec in bad:
        raises(ValueError, tk.parse_range, spec, TODAY)
    print("  OK")


# ------------------------------------------------------------ resolve_tickers

def test_resolve_tickers_formats():
    print("Testing resolve_tickers formats...")
    want = ["AAPL", "GOOGL"]
    for spec in ("AAPL,GOOGL", "[AAPL, GOOGL]", ["AAPL", "GOOGL"],
                 '"AAPL", "GOOGL"', "AAPL GOOGL", " aapl ,  googl ", "['AAPL', 'GOOGL']",
                 '["AAPL", "GOOGL"]', ("AAPL", "GOOGL")):
        assert tk.resolve_tickers(spec) == want, spec
    assert tk.resolve_tickers(["aapl", "goog"]) == ["AAPL", "GOOG"]
    assert tk.resolve_tickers("aapl,GOOGL,AAPL, msft,googl") == ["AAPL", "GOOGL", "MSFT"]
    print("  OK")


def test_resolve_tickers_rejects_empty():
    print("Testing resolve_tickers empty...")
    for spec in ("", None, "  ", [], "[]", ",,"):
        raises(ValueError, tk.resolve_tickers, spec)
    print("  OK")


def test_resolve_current_qqq_uses_the_newest_snapshot_in_weight_order():
    print("Testing CurrentQQQ...")
    with temp_qqq() as folder:
        write_snapshot(folder, "2026-10-29", {"OLD": holding("Old", 9.0), "AAPL": holding("Apple", 5.0)})
        write_snapshot(folder, "2026-10-30", {"AAPL": holding("Apple", 5.0), "NVDA": holding("Nvidia", 9.0),
                                               "NEW": holding("New", 0.4)})
        for spec in ("CurrentQQQ", "currentqqq", "CURRENTQQQ", " CurrentQQQ "):
            assert tk.resolve_tickers(spec) == ["NVDA", "AAPL", "NEW"], spec
    print("  OK")


def test_resolve_current_qqq_without_a_snapshot():
    print("Testing CurrentQQQ without a snapshot...")
    with temp_qqq():
        exc = raises(ValueError, tk.resolve_tickers, "CurrentQQQ")
        assert "/webhook/qqqholdings/run" in str(exc), exc
    print("  OK")


# ----------------------------------------------------------------- to_records

def test_to_records_shape_and_rounding():
    print("Testing to_records...")
    df = frame(("2026-11-10", 10.123456, 11.99999, 9.00004, 10.5, 1000.0))
    out = tk.to_records(df)
    assert out == [rec("2026-11-10", 10.1235, 12.0, 9.0, 10.5, 1000)], out
    assert type(out[0]["volume"]) is int and type(out[0]["open"]) is float
    assert list(out[0]) == ["date", "open", "high", "low", "close", "volume"]
    print("  OK")


def test_to_records_drops_nan_rows_and_handles_empty():
    print("Testing to_records NaN and empty...")
    nan = float("nan")
    df = frame(("2026-11-09", 1.0, 2.0, 0.5, 1.5, 10), ("2026-11-10", nan, 2.0, 0.5, 1.5, 10),
               ("2026-11-11", 1.0, 2.0, 0.5, 1.5, nan), ("2026-11-12", 1.0, 2.0, 0.5, 1.5, 20))
    assert [r["date"] for r in tk.to_records(df)] == ["2026-11-09", "2026-11-12"]
    assert tk.to_records(df.iloc[0:0]) == []
    assert tk.to_records(pd.DataFrame()) == []
    assert tk.to_records(None) == []
    print("  OK")


# ------------------------------------------------------------------ _download

def test_download_passes_the_right_arguments_to_yfinance():
    print("Testing _download...")
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        def history(self, **kwargs):
            seen["kwargs"] = kwargs
            return "FRAME"

    fake = types.ModuleType("yfinance")
    fake.Ticker = FakeTicker
    with mock.patch.dict(sys.modules, {"yfinance": fake}):
        out = tk._download("AAPL", D(2026, 3, 1), D(2026, 3, 31))
    assert out == "FRAME"
    assert seen["symbol"] == "AAPL", seen
    assert seen["kwargs"] == {"start": "2026-03-01", "end": "2026-04-01",
                              "interval": "1d", "auto_adjust": False,
                              "raise_errors": True}, seen
    with mock.patch.dict(sys.modules, {"yfinance": fake}):
        tk._download("AAPL", D(2026, 12, 31), D(2026, 12, 31))
    assert seen["kwargs"]["end"] == "2027-01-01", seen
    print("  OK")


# --------------------------------------------------------------- fetch_ticker

def test_fetch_ticker_saves_the_expected_file():
    print("Testing fetch_ticker save...")
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download({"AAPL": DF1})):
        res = tk.fetch_ticker("AAPL", D(2026, 11, 10), D(2026, 11, 11))
        assert res == {"ticker": "AAPL", "status": "saved", "fetched": 2, "added": 2, "total": 2,
                       "first": "2026-11-10", "last": "2026-11-11"}, res
        text = (t.dir / "AAPL.json").read_text(encoding="utf-8")
        assert text == json.dumps(REC1, indent=2), text
        assert t.files() == ["AAPL.json"], t.files()   # no leftover temp file
    print("  OK")


def test_fetch_ticker_merges_into_an_existing_file():
    print("Testing fetch_ticker merge...")
    second = frame(("2026-11-11", 20.0, 21.0, 19.0, 20.5, 5000),
                   ("2026-11-12", 21.0, 22.0, 20.0, 21.5, 6000))
    with temp_ohlcv() as t:
        with mock.patch.object(tk, "_download", fake_download({"AAPL": DF1})):
            tk.fetch_ticker("AAPL", D(2026, 11, 10), D(2026, 11, 11))
        with mock.patch.object(tk, "_download", fake_download({"AAPL": second})):
            res = tk.fetch_ticker("AAPL", D(2026, 11, 11), D(2026, 11, 12))
        assert res == {"ticker": "AAPL", "status": "saved", "fetched": 2, "added": 1, "total": 3,
                       "first": "2026-11-10", "last": "2026-11-12"}, res
        saved = json.loads((t.dir / "AAPL.json").read_text())
        assert saved == [REC1[0], rec("2026-11-11", 20.0, 21.0, 19.0, 20.5, 5000),
                         rec("2026-11-12", 21.0, 22.0, 20.0, 21.5, 6000)], saved
    print("  OK")


def test_fetch_ticker_empty_frame_fails_without_a_file():
    print("Testing fetch_ticker empty...")
    for empty in (pd.DataFrame(), None, DF1.iloc[0:0]):
        with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download({"ZZZZ": empty})):
            res = tk.fetch_ticker("ZZZZ", D(2026, 11, 10), D(2026, 11, 11))
            assert res["status"] == "failed" and "no data" in res["reason"], res
            assert t.everything() == [], t.everything()
    print("  OK")


def test_fetch_ticker_download_error_fails_without_a_file():
    print("Testing fetch_ticker download error...")
    with temp_ohlcv() as t, \
            mock.patch.object(tk, "_download", fake_download({"AAPL": RuntimeError("Yahoo said no")})):
        res = tk.fetch_ticker("AAPL", D(2026, 11, 10), D(2026, 11, 11))
        assert res["status"] == "failed" and "Yahoo said no" in res["reason"], res
        assert "RuntimeError" in res["reason"], res
        assert t.everything() == [], t.everything()
    print("  OK")


def test_fetch_ticker_rejects_path_like_symbols():
    print("Testing fetch_ticker invalid symbols...")
    dl = fake_download({})
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", dl):
        for symbol in ("../x", "A/B", ".hidden", "", "A B", "..", "A\\B", "X" * 40):
            res = tk.fetch_ticker(symbol, D(2026, 11, 10), D(2026, 11, 11))
            assert res["status"] == "failed" and "valid ticker" in res["reason"], (symbol, res)
        assert dl.calls == [], dl.calls
        assert t.everything() == [], t.everything()
    print("  OK")


def test_fetch_ticker_leaves_a_corrupt_file_alone():
    print("Testing fetch_ticker corrupt file...")
    for damage in ("{oops", "[1]", '{"date": "x"}', '[{"close": 1}]'):
        with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download({"AAPL": DF1})):
            t.dir.mkdir(parents=True)
            (t.dir / "AAPL.json").write_text(damage, encoding="utf-8")
            res = tk.fetch_ticker("AAPL", D(2026, 11, 10), D(2026, 11, 11))
            assert res["status"] == "failed", (damage, res)
            assert (t.dir / "AAPL.json").read_text(encoding="utf-8") == damage
            assert t.files() == ["AAPL.json"], t.files()
    print("  OK")


# ------------------------------------------------------------------------ run

PAYLOAD = {"range": "30d", "tickers": "AAPL,GOOGL"}


def test_run_all_good():
    print("Testing run ok...")
    table = {"AAPL": DF1, "GOOGL": DF1}
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download(table)):
        res = tk.run(PAYLOAD)
        assert res["status"] == "ok", res
        assert res["requested"] == 2 and [r["ticker"] for r in res["saved"]] == ["AAPL", "GOOGL"], res
        assert res["failed"] == [], res
        assert res["end"] == datetime.date.today().isoformat(), res
        assert t.files() == ["AAPL.json", "GOOGL.json"], t.files()
    print("  OK")


def test_run_partial_reports_each_ticker_in_order():
    print("Testing run partial...")
    table = {"AAA": DF1, "BBB": pd.DataFrame(), "CCC": RuntimeError("boom"), "DDD": DF1}
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download(table)):
        res = tk.run({"range": "2026-11-01..2026-11-12", "tickers": "AAA,BBB,CCC,DDD"})
        assert res["status"] == "partial", res
        assert [r["ticker"] for r in res["saved"]] == ["AAA", "DDD"], res
        assert [r["ticker"] for r in res["failed"]] == ["BBB", "CCC"], res
        assert res["start"] == "2026-11-01" and res["end"] == "2026-11-12", res
        lines = res["message"].splitlines()
        assert len(lines) == 5, lines
        assert [ln.split()[0] for ln in lines[:4]] == ["AAA", "BBB", "CCC", "DDD"], lines
        assert "saved" in lines[0] and "FAILED" in lines[1] and "boom" in lines[2], lines
        assert lines[4] == "OHLCV 2026-11-01 .. 2026-11-12: 2 of 4 saved, 2 failed", lines
        assert t.log.call_args_list == [
            mock.call("ticker_ohlcv", lines[4]),
            mock.call("ticker_ohlcv", "failed: BBB (no data for 2026-11-01 .. 2026-11-12 "
                                      "(no trading days in the range)); CCC (RuntimeError: boom)")], t.log.call_args_list
    print("  OK")


def test_run_reports_a_yahoo_rate_limit_as_that_tickers_failure():
    print("Testing run with a rate limit...")

    class YFRateLimitError(Exception):
        pass

    table = {"AAA": YFRateLimitError("Too Many Requests"), "BBB": DF1}
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download(table)):
        res = tk.run({"range": "5d", "tickers": "AAA,BBB"})
        assert res["status"] == "partial", res
        assert res["failed"][0]["ticker"] == "AAA", res
        assert res["failed"][0]["reason"].startswith("YFRateLimitError:"), res
        assert [r["ticker"] for r in res["saved"]] == ["BBB"], res
        assert t.files() == ["BBB.json"], t.files()
    print("  OK")


def test_run_none_saved_is_an_error():
    print("Testing run none saved...")
    table = {"AAA": pd.DataFrame(), "BBB": RuntimeError("x")}
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download(table)):
        res = tk.run({"range": "5d", "tickers": ["AAA", "BBB"]})
        assert res["status"] == "error" and res["saved"] == [] and len(res["failed"]) == 2, res
        assert t.everything() == [], t.everything()
    print("  OK")


def test_run_bad_arguments_never_download():
    print("Testing run bad arguments...")
    dl = fake_download({"AAPL": DF1})
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", dl):
        for payload in ({}, None, {"tickers": "AAPL"}, {"range": "30d"}, {"range": "xx", "tickers": "AAPL"},
                        {"range": "30d", "tickers": ""}, {"range": "30d", "tickers": []},
                        {"range": "2026-12-31..", "tickers": "AAPL"}):
            res = tk.run(payload)
            assert res["status"] == "error" and res["message"], (payload, res)
        assert dl.calls == [], dl.calls
        t.log.assert_not_called()
    print("  OK")


def test_run_accepts_webhook_style_payloads():
    print("Testing run webhook payloads...")
    dl = fake_download({"AAPL": DF1, "GOOGL": DF1})
    with temp_ohlcv(), mock.patch.object(tk, "_download", dl):
        res = tk.run({"range": 30, "tickers": ["AAPL", "GOOGL"]})
        assert res["status"] == "ok", res
        today = datetime.date.today()
        assert dl.calls[0] == ("AAPL", today - datetime.timedelta(days=30), today), dl.calls
    print("  OK")


def test_run_current_qqq():
    print("Testing run CurrentQQQ...")
    dl = fake_download({"NVDA": DF1, "AAPL": DF1})
    with temp_ohlcv(), temp_qqq() as folder, mock.patch.object(tk, "_download", dl):
        write_snapshot(folder, "2026-10-30", {"AAPL": holding("Apple", 5.0), "NVDA": holding("Nvidia", 9.0)})
        res = tk.run({"range": "7d", "tickers": "CurrentQQQ"})
        assert res["status"] == "ok" and [c[0] for c in dl.calls] == ["NVDA", "AAPL"], (res, dl.calls)
    print("  OK")


def test_run_survives_a_logging_failure():
    print("Testing run with failing log...")
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download({"AAPL": DF1})):
        t.log.side_effect = RuntimeError("log broken")
        assert tk.run({"range": "30d", "tickers": "AAPL"})["status"] == "ok"
    print("  OK")


# ----------------------------------------------------------------------- main

def _main(argv, table):
    out = io.StringIO()
    dl = fake_download(table)
    with mock.patch.object(tk, "_download", dl), contextlib.redirect_stdout(out):
        code = tk.main(argv)
    return code, out.getvalue(), dl


def test_main_success_prints_the_summary():
    print("Testing main ok...")
    with temp_ohlcv():
        code, out, _ = _main(["30d", "AAPL,GOOGL"], {"AAPL": DF1, "GOOGL": DF1})
        assert code == 0, code
        assert "AAPL" in out and "2 of 2 saved" in out, out
    print("  OK")


def test_main_failure_returns_one():
    print("Testing main failure...")
    with temp_ohlcv():
        code, out, _ = _main(["30d", "AAPL,ZZZZ"], {"AAPL": DF1, "ZZZZ": pd.DataFrame()})
        assert code == 1, code
        assert "FAILED" in out and "1 of 2 saved, 1 failed" in out, out
        code, out, dl = _main(["nonsense", "AAPL"], {})
        assert code == 1 and dl.calls == [], (code, dl.calls)
    print("  OK")


def test_main_requires_tickers():
    print("Testing main missing tickers...")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        exc = raises(SystemExit, tk.main, ["30d"])
    assert exc.code == 2, exc.code
    print("  OK")


def test_main_takes_space_separated_tickers():
    print("Testing main space separated...")
    with temp_ohlcv() as t:
        code, out, dl = _main(["30d", "AAPL", "GOOGL"], {"AAPL": DF1, "GOOGL": DF1})
        assert code == 0 and [c[0] for c in dl.calls] == ["AAPL", "GOOGL"], (code, dl.calls)
        assert t.files() == ["AAPL.json", "GOOGL.json"], t.files()
    print("  OK")


# --------------------------------------------------------------- registration

def test_webhook_and_switch_are_registered():
    print("Testing registration...")
    config = json.loads((ROOT / "configs" / "config.json").read_text(encoding="utf-8"))
    entries = [w for w in config["webhooks"] if w["path"] == "/webhook/ticker/ohlcv/fetch"]
    assert len(entries) == 1, entries
    assert entries[0]["module"] == "jobs.ticker_ohlcv" and entries[0]["function"] == "run", entries
    assert entries[0]["require_secret"] is True, entries
    assert hasattr(tk, "run")
    switches = json.loads((ROOT / "configs" / "log_switches.json").read_text(encoding="utf-8"))
    assert switches["types"]["ticker_ohlcv"] is True, switches
    assert tk.LOG_TYPE == "ticker_ohlcv"
    print("  OK")


def test_importing_the_module_does_not_import_yfinance():
    print("Testing yfinance stays lazy...")
    import subprocess
    code = "import sys; import jobs.ticker_ohlcv; assert 'yfinance' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
    print("  OK")


# ---------------------------------------------------------------- fetch_bars

def test_fetch_bars_returns_records():
    print("Testing fetch_bars...")
    dl = fake_download({"AAPL": DF1, "ZZZZ": pd.DataFrame()})
    with mock.patch.object(tk, "_download", dl):
        assert tk.fetch_bars("AAPL", D(2026, 11, 10), D(2026, 11, 11)) == REC1
        assert tk.fetch_bars("ZZZZ", D(2026, 11, 10), D(2026, 11, 11)) == []
    assert dl.calls[0] == ("AAPL", D(2026, 11, 10), D(2026, 11, 11)), dl.calls
    print("  OK")


# ------------------------------------------------------------- parse_request

def test_parse_request_returns_start_end_and_symbols():
    print("Testing parse_request...")
    start, end, symbols = tk.parse_request({"range": "2026-11-01..2026-11-12", "tickers": "aapl, GOOGL"})
    assert (start, end, symbols) == (D(2026, 11, 1), D(2026, 11, 12), ["AAPL", "GOOGL"]), (start, end, symbols)
    start, end, symbols = tk.parse_request({"range": "7d", "tickers": ["AAPL"]})
    assert end == datetime.date.today() and start == end - datetime.timedelta(days=7), (start, end)
    for payload in ({}, {"tickers": "AAPL"}, {"range": "xx", "tickers": "AAPL"},
                    {"range": "7d"}, {"range": "7d", "tickers": ""}, {"range": "7d", "tickers": []}):
        raises(ValueError, tk.parse_request, payload)
    print("  OK")


# ---------------------------------------------------------------- fetch_many

def test_fetch_many_reports_what_run_reports():
    print("Testing fetch_many...")
    with temp_ohlcv() as t, mock.patch.object(tk, "_download", fake_download({"AAA": DF1, "BBB": RuntimeError("x")})):
        res = tk.fetch_many(D(2026, 11, 10), D(2026, 11, 11), ["AAA", "BBB"])
        assert res["status"] == "partial" and res["requested"] == 2, res
        assert res["start"] == "2026-11-10" and res["end"] == "2026-11-11", res
        assert [r["ticker"] for r in res["saved"]] == ["AAA"] and [r["ticker"] for r in res["failed"]] == ["BBB"], res
        assert "no_data" not in res["failed"][0], res
    print("  OK")


# ------------------------------------------------------------------ run_daily

class YFPricesMissingError(Exception):
    """Named like yfinance's, which raises it for ANY Yahoo error response (a bad crumb, say)."""


SNAPSHOT = {"AAPL": holding("Apple", 5.0), "NVDA": holding("Nvidia", 9.0), "MSFT": holding("Microsoft", 7.0)}
SNAPSHOT_ORDER = ["NVDA", "MSFT", "AAPL"]
LOOKBACK = datetime.timedelta(days=7)


def _daily(table, today=TODAY, snapshot=SNAPSHOT):
    dl = fake_download(table)
    with temp_ohlcv() as t, temp_qqq() as folder, mock.patch.object(tk, "_download", dl):
        write_snapshot(folder, "2026-10-30", snapshot)
        res = tk.run_daily(today=today)
        return res, dl, t, t.everything(), t.log


def test_run_daily_asks_for_the_last_seven_days_for_every_ticker():
    print("Testing run_daily ok...")
    assert tk.DAILY_LOOKBACK_DAYS == 7
    res, dl, t, _, _ = _daily({s: DF1 for s in SNAPSHOT})
    assert res["status"] == "ok" and res["requested"] == 3, res
    assert dl.calls == [(s, TODAY - LOOKBACK, TODAY) for s in SNAPSHOT_ORDER], dl.calls
    assert res["start"] == "2026-11-06" and res["end"] == "2026-11-13", res
    print("  OK")


def test_run_daily_a_few_failures_are_partial():
    print("Testing run_daily partial...")
    table = {"NVDA": DF1, "MSFT": ConnectionError("down"), "AAPL": DF1}
    res, _, _, _, _ = _daily(table)
    assert res["status"] == "partial" and [r["ticker"] for r in res["failed"]] == ["MSFT"], res
    assert "no_data" not in res["failed"][0], res
    print("  OK")


def test_run_daily_a_market_holiday_is_ok():
    print("Testing run_daily holiday...")
    # Every ticker has the earlier days but nothing for the last one: just ok, nothing special.
    res, dl, t, written, _ = _daily({s: DF1 for s in SNAPSHOT})
    assert res["status"] == "ok" and res["failed"] == [], res
    assert all(r["last"] == "2026-11-11" for r in res["saved"]), res
    print("  OK")


def test_run_daily_a_yahoo_outage_is_an_error_not_a_holiday():
    print("Testing run_daily Yahoo outage...")
    outage = YFPricesMissingError('(Yahoo error = "Invalid Crumb")')
    res, _, t, written, _ = _daily({s: outage for s in SNAPSHOT})
    assert res["status"] == "error" and len(res["failed"]) == 3 and res["saved"] == [], res
    assert all(r["reason"].startswith("YFPricesMissingError:") and "Invalid Crumb" in r["reason"]
               for r in res["failed"]), res
    assert [p for p in written if p.startswith("stock/ohlcv")] == [], written
    print("  OK")


def test_run_daily_every_ticker_failing_is_an_error_so_the_scheduler_retries():
    print("Testing run_daily errors...")
    res, _, t, written, _ = _daily({s: ConnectionError("down") for s in SNAPSHOT})
    assert res["status"] == "error" and len(res["failed"]) == 3, res
    assert [p for p in written if p.startswith("stock/ohlcv")] == [], written
    print("  OK")


def test_run_daily_without_a_saved_qqq_list_is_an_error():
    print("Testing run_daily without a snapshot...")
    dl = fake_download({})
    with temp_ohlcv(), temp_qqq(), mock.patch.object(tk, "_download", dl):
        res = tk.run_daily(today=TODAY)
        assert res["status"] == "error" and "/webhook/qqqholdings/run" in res["message"], res
        assert dl.calls == [], dl.calls
    print("  OK")


def test_run_daily_with_an_empty_qqq_list_says_so():
    print("Testing run_daily with an empty list...")
    res, dl, _, _, _ = _daily({}, snapshot={})
    assert res == {"status": "error", "message": "the saved QQQ holdings list is empty"}, res
    assert dl.calls == [], dl.calls
    print("  OK")


def test_each_daily_run_heals_the_days_an_earlier_run_missed():
    print("Testing run_daily self-healing...")
    bars = {"NVDA": ["2026-11-12", "2026-11-13"], "MSFT": ["2026-11-12", "2026-11-13"]}

    def download(symbol, start, end):
        if failing and symbol == failing[0]:
            raise ConnectionError("down")
        days = [d for d in bars[symbol] if start.isoformat() <= d <= end.isoformat()]
        return frame(*[(d, 1.0, 2.0, 0.5, 1.5, 10) for d in days]) if days else pd.DataFrame()

    failing = ["MSFT"]
    with temp_ohlcv() as t, temp_qqq() as folder, mock.patch.object(tk, "_download", download):
        write_snapshot(folder, "2026-10-30", {"NVDA": holding("Nvidia", 9.0), "MSFT": holding("Microsoft", 7.0)})
        day1 = tk.run_daily(today=D(2026, 11, 12))
        assert day1["status"] == "partial" and [r["ticker"] for r in day1["failed"]] == ["MSFT"], day1
        assert t.files() == ["NVDA.json"], t.files()
        failing.clear()
        day2 = tk.run_daily(today=D(2026, 11, 13))
        assert day2["status"] == "ok", day2
        assert [r["date"] for r in json.loads((t.dir / "MSFT.json").read_text())] == ["2026-11-12", "2026-11-13"]
        assert [r["date"] for r in json.loads((t.dir / "NVDA.json").read_text())] == ["2026-11-12", "2026-11-13"]
    print("  OK")


def test_failed_tickers_get_one_log_entry_capped_at_ten():
    print("Testing the failed-ticker log entry...")
    names = [f"T{n:02d}" for n in range(1, 14)]
    snapshot = {name: holding(name, 100.0 - n) for n, name in enumerate(names)}

    def fails(count):
        return {name: (ConnectionError(f"down {name}") if n < count else DF1) for n, name in enumerate(names)}

    res, _, _, _, log = _daily(fails(0), snapshot=snapshot)
    assert log.call_count == 1, log.call_args_list      # nothing failed: only the summary line

    res, _, _, _, log = _daily(fails(2), snapshot=snapshot)
    assert log.call_args_list == [
        mock.call("ticker_ohlcv", "OHLCV 2026-11-06 .. 2026-11-13: 11 of 13 saved, 2 failed"),
        mock.call("ticker_ohlcv", "failed: T01 (ConnectionError: down T01); T02 (ConnectionError: down T02)")
    ], log.call_args_list

    res, _, _, _, log = _daily(fails(12), snapshot=snapshot)
    assert log.call_count == 2, log.call_args_list
    text = log.call_args_list[1][0][1]
    assert text == "failed: " + "; ".join(f"T{n:02d} (ConnectionError: down T{n:02d})"
                                         for n in range(1, 11)) + "; +2 more", text
    assert "T11" not in text and "T12" not in text, text

    res, _, _, _, log = _daily(fails(10), snapshot=snapshot)
    assert not log.call_args_list[1][0][1].endswith("more"), log.call_args_list
    print("  OK")


def test_run_daily_uses_the_schedule_date_when_given_no_date():
    print("Testing run_daily default date...")
    dl = fake_download({s: DF1 for s in SNAPSHOT})
    with temp_ohlcv(), temp_qqq() as folder, mock.patch.object(tk, "_download", dl), \
            mock.patch.object(tk, "_schedule_today", return_value=D(2026, 11, 13)):
        write_snapshot(folder, "2026-10-30", SNAPSHOT)
        assert tk.run_daily({})["status"] == "ok"
        assert dl.calls[0] == ("NVDA", D(2026, 11, 6), D(2026, 11, 13)), dl.calls
    print("  OK")


def test_schedule_today_is_the_schedule_zone_whatever_the_process_zone_is():
    print("Testing _schedule_today time zone...")
    from zoneinfo import ZoneInfo
    import os
    import time
    la_evening = datetime.datetime(2026, 11, 13, 21, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    class FakeDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):    # the real clock, frozen at la_evening
            return la_evening.astimezone(tz) if tz else la_evening.astimezone()

    class FakeDate(datetime.date):
        @classmethod
        def today(cls):           # the process zone's date, like the real date.today()
            return FakeDateTime.now().date()

    fake_module = types.SimpleNamespace(datetime=FakeDateTime, date=FakeDate, timedelta=datetime.timedelta)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # 21:30 Pacific on the 13th is already 00:30 on the 14th in New York.
        assert la_evening.astimezone().date() == D(2026, 11, 14), la_evening.astimezone()
        assert FakeDate.today() == D(2026, 11, 14)
        assert tk._schedule_today(now=la_evening) == D(2026, 11, 13)
        with mock.patch.object(tk, "datetime", fake_module):
            assert tk._schedule_today() == D(2026, 11, 13)
            assert tk.datetime.date.today() != D(2026, 11, 13)   # plain date.today() would be a day ahead
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    print("  OK")


def test_schedule_file_loads():
    print("Testing configs/ticker_ohlcv_schedule.json...")
    import jobs.scheduler as sched
    s = sched.load_schedule(tk.SCHEDULE_PATH)
    assert s.run_time == datetime.time(14, 30) and str(s.tz) == "America/Los_Angeles", s
    assert s.weekdays_only is True and s.catch_up_until == datetime.time(23, 0), s
    assert tk.SCHEDULE_PATH == ROOT / "configs" / "ticker_ohlcv_schedule.json", tk.SCHEDULE_PATH
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} OHLCV tests passed!")
