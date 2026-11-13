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
import tempfile
import threading
import time
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

import jobs.qqqholdings_store as qs
import jobs.ticker_ohlcv as tk

TODAY = datetime.date(2026, 11, 13)
D = datetime.date


class temp_ohlcv:
    """Point the module at a temp stock/ohlcv dir and mock its logger."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / "stock" / "ohlcv"
        self._patches = [mock.patch.object(tk, "OHLCV_DIR", self.dir),
                         mock.patch.object(tk, "write_log")]
        started = [p.start() for p in self._patches]
        self.log = started[1]
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        return False

    def files(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.is_dir() else []

    def everything(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))


def frame(*rows):
    """A yfinance-like history frame from (date, open, high, low, close, volume) rows."""
    index = pd.DatetimeIndex([r[0] for r in rows], tz="America/New_York")
    return pd.DataFrame({
        "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
        "Adj Close": [r[4] for r in rows], "Volume": [r[5] for r in rows],
        "Dividends": [0.0] * len(rows), "Stock Splits": [0.0] * len(rows),
    }, index=index)


def rec(date, o, h, l, c, v):
    return {"date": date, "open": o, "high": h, "low": l, "close": c, "volume": v}


DF1 = frame(("2026-11-10", 10.0, 11.0, 9.0, 10.5, 1000), ("2026-11-11", 10.5, 12.0, 10.0, 11.0, 2000))
REC1 = [rec("2026-11-10", 10.0, 11.0, 9.0, 10.5, 1000), rec("2026-11-11", 10.5, 12.0, 10.0, 11.0, 2000)]


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


def raises(exc_type, fn, *args):
    try:
        fn(*args)
    except exc_type as exc:
        return exc
    raise AssertionError(f"{fn.__name__}{args} did not raise {exc_type.__name__}")


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


def _write_snapshot(folder, as_of, holdings):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"qqqholdings_{as_of}.json").write_text(
        json.dumps({"as_of": as_of, "holdings": holdings}), encoding="utf-8")


def _h(name, weight):
    return {"name": name, "weight": weight, "shares": 1.0}


@contextlib.contextmanager
def temp_qqq():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "stock" / "qqqholdings"
        with mock.patch.object(qs, "STOCK_DIR", folder):
            yield folder


def test_resolve_current_qqq_uses_the_newest_snapshot_in_weight_order():
    print("Testing CurrentQQQ...")
    with temp_qqq() as folder:
        _write_snapshot(folder, "2026-10-29", {"OLD": _h("Old", 9.0), "AAPL": _h("Apple", 5.0)})
        _write_snapshot(folder, "2026-10-30", {"AAPL": _h("Apple", 5.0), "NVDA": _h("Nvidia", 9.0),
                                               "NEW": _h("New", 0.4)})
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


# -------------------------------------------------------------- merge_records

def test_merge_records():
    print("Testing merge_records...")
    old = [rec("2026-11-12", 3, 3, 3, 3, 3), rec("2026-11-10", 1, 1, 1, 1, 1),
           rec("2026-11-11", 2, 2, 2, 2, 2)]
    new = [rec("2026-11-13", 9, 9, 9, 9, 9), rec("2026-11-11", 5, 5, 5, 5, 5)]
    out = tk.merge_records(old, new)
    assert [r["date"] for r in out] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"], out
    assert out[1] == rec("2026-11-11", 5, 5, 5, 5, 5), out
    assert out[0]["open"] == 1 and out[2]["open"] == 3
    assert tk.merge_records([], new) == sorted(new, key=lambda r: r["date"])
    assert tk.merge_records(old, []) == sorted(old, key=lambda r: r["date"])
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


def test_symbol_re_accepts_real_symbol_shapes():
    print("Testing SYMBOL_RE real symbol shapes...")
    for symbol in ("AAPL", "BRK-B", "BF.B", "^GSPC", "EURUSD=X", "QQQ"):
        assert tk.SYMBOL_RE.match(symbol), symbol
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
        t.log.assert_called_once_with("ticker_ohlcv", lines[4])
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
        _write_snapshot(folder, "2026-10-30", {"AAPL": _h("Apple", 5.0), "NVDA": _h("Nvidia", 9.0)})
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


# ---------------------------------------------------------------- concurrency

def test_concurrent_fetches_of_one_ticker_keep_every_date():
    print("Testing concurrent fetches...")
    real = tk.write_atomic

    def slow(path, text):
        time.sleep(0.03)
        real(path, text)

    days = [f"2026-11-{n:02d}" for n in range(1, 9)]
    frames = [frame((day, 1.0, 2.0, 0.5, 1.5, 10)) for day in days]
    errors = []
    by_thread = {}   # each worker's frame, found by thread (patching per thread would race)

    def download(symbol, start, end):
        return by_thread[threading.get_ident()]

    def worker(df):
        try:
            by_thread[threading.get_ident()] = df
            res = tk.fetch_ticker("AAPL", D(2026, 11, 1), D(2026, 11, 8))
            assert res["status"] == "saved", res
        except BaseException as exc:  # reported below, not lost in the thread
            errors.append(exc)

    with temp_ohlcv() as t, mock.patch.object(tk, "write_atomic", side_effect=slow), \
            mock.patch.object(tk, "_download", download):
        threads = [threading.Thread(target=worker, args=(df,)) for df in frames]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        saved = json.loads((t.dir / "AAPL.json").read_text())
        assert [r["date"] for r in saved] == days, saved
        assert t.files() == ["AAPL.json"], t.files()
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} OHLCV tests passed!")
