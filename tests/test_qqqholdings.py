#!/usr/bin/env python3
"""Tests for the QQQ holdings daily report (jobs/qqqholdings_*.py).

Nothing touches the network or the repo: the fetcher is mocked, stock/ is
redirected to a temp dir, and the logging engine is mocked:

    python3 tests/test_qqqholdings.py
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.qqqholdings_diff as qd
import jobs.qqqholdings_fetch as qf
import jobs.qqqholdings_job as qj


def _h(name, weight, shares=0.0):
    return {"name": name, "weight": weight, "shares": shares}


def test_diff_added_and_removed():
    """Tickers only in the new list are added; only in the old list, removed."""
    print("Testing diff added/removed...")
    old = {"AAA": _h("Aaa Inc", 5.0), "OLD": _h("Old Co", 0.5)}
    new = {"AAA": _h("Aaa Inc", 5.0), "NEW": _h("New Co", 0.4)}
    d = qd.diff_holdings(old, new)
    assert d["added"] == [{"ticker": "NEW", "name": "New Co", "weight": 0.4}], d
    assert d["removed"] == [{"ticker": "OLD", "name": "Old Co", "weight": 0.5}], d
    assert d["changed"] == [], d
    print("  OK")


def test_diff_weight_changes_sorted_by_size():
    """Changes are in percentage points, biggest move first."""
    print("Testing diff weight changes...")
    old = {"A": _h("A", 5.00), "B": _h("B", 3.00), "C": _h("C", 2.00)}
    new = {"A": _h("A", 5.50), "B": _h("B", 2.00), "C": _h("C", 2.00)}
    d = qd.diff_holdings(old, new)
    assert [c["ticker"] for c in d["changed"]] == ["B", "A"], d
    assert d["changed"][0] == {
        "ticker": "B", "name": "B", "old_weight": 3.0, "new_weight": 2.0, "change": -1.0,
    }, d
    assert d["changed"][1]["change"] == 0.5, d
    assert d["added"] == [] and d["removed"] == [], d
    print("  OK")


def test_diff_ignores_changes_below_threshold():
    print("Testing diff threshold...")
    old = {"A": _h("A", 3.000)}
    new = {"A": _h("A", 3.005)}
    assert qd.diff_holdings(old, new)["changed"] == []
    loose = qd.diff_holdings(old, new, threshold=0.001)["changed"]
    assert len(loose) == 1 and loose[0]["change"] == 0.005, loose
    print("  OK")


def test_diff_threshold_is_inclusive():
    """A move of exactly the threshold counts (5.01 - 5.00 is 0.00999... in floats)."""
    print("Testing diff threshold boundary...")
    d = qd.diff_holdings({"A": _h("A", 5.00)}, {"A": _h("A", 5.01)})
    assert len(d["changed"]) == 1 and d["changed"][0]["change"] == 0.01, d
    print("  OK")


def test_diff_ties_are_ordered_by_ticker():
    """Equal weights / equal moves must not depend on the input row order."""
    print("Testing diff tie order...")
    new = {"ZZZ": _h("Z", 1.0), "AAA": _h("A", 1.0), "MMM": _h("M", 2.0)}
    d = qd.diff_holdings({}, new)
    assert [r["ticker"] for r in d["added"]] == ["MMM", "AAA", "ZZZ"], d
    d = qd.diff_holdings(new, {})
    assert [r["ticker"] for r in d["removed"]] == ["MMM", "AAA", "ZZZ"], d
    old = {"ZZZ": _h("Z", 1.0), "AAA": _h("A", 1.0)}
    moved = {"ZZZ": _h("Z", 1.5), "AAA": _h("A", 1.5)}
    d = qd.diff_holdings(old, moved)
    assert [r["ticker"] for r in d["changed"]] == ["AAA", "ZZZ"], d
    print("  OK")


def test_diff_identical_lists_are_empty():
    print("Testing diff of identical lists...")
    book = {"A": _h("A", 5.0), "B": _h("B", 3.0)}
    d = qd.diff_holdings(book, dict(book))
    assert d == {"added": [], "removed": [], "changed": []}, d
    print("  OK")


SAMPLE_RESPONSE = {
    "cusip": "46090E103",
    "effectiveDate": "2026-11-01",           # a Sunday
    "effectiveBusinessDate": "2026-10-30",   # the last trading day
    "totalNumberOfHoldings": 8,
    "holdings": [
        {"ticker": "AAPL", "issuerName": "Apple Inc", "units": 109972726,
         "percentageOfTotalNetAssets": 8.169369,
         "securityTypeName": "Common Stock", "securityTypeCode": "COM"},
        {"ticker": "NVDA", "issuerName": "NVIDIA Corp", "units": 181602774,
         "percentageOfTotalNetAssets": 8.010571,
         "securityTypeName": "Common Stock", "securityTypeCode": "COM"},
        {"ticker": "ASML", "issuerName": "ASML Holding NV", "units": 1955770,
         "percentageOfTotalNetAssets": 0.661015,
         "securityTypeName": "American Depository Receipt - NY", "securityTypeCode": "DRNY"},
        {"ticker": "USD", "issuerName": "CASH &amp; EQUIVALENTS", "units": 687936948.36,
         "percentageOfTotalNetAssets": 0.148286,
         "securityTypeName": "Currency", "securityTypeCode": "CURR"},
        {"ticker": "NQZ6", "issuerName": "CME E-Mini NASDAQ 100 Index Future", "units": 1125,
         "percentageOfTotalNetAssets": 0.138387,
         "securityTypeName": "Index Future", "securityTypeCode": "IFUT"},
        {"ticker": None, "issuerName": "CASH COLLATERAL", "units": 40277847,
         "percentageOfTotalNetAssets": 0.008682,
         "securityTypeName": "Currency Collateral", "securityTypeCode": "CURRCOL"},
        {"ticker": None, "issuerName": "CONTRA FUTURE FUTURE DEC 18 26", "units": -1125,
         "percentageOfTotalNetAssets": -0.138387,
         "securityTypeName": "Synthetic Cash", "securityTypeCode": "SYN"},
        {"ticker": "USDPDV", "issuerName": "USD Pending Dividends", "units": 0,
         "percentageOfTotalNetAssets": None,
         "securityTypeName": "Currency", "securityTypeCode": "CURR"},
    ],
}


def _stock_row(ticker, weight, units=1, name=None, code="COM"):
    return {"ticker": ticker, "issuerName": name or ticker, "units": units,
            "percentageOfTotalNetAssets": weight,
            "securityTypeName": "Common Stock", "securityTypeCode": code}


class _Resp:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.data


class _Session:
    """Replays a list of responses; an Exception in the list is raised."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def test_parse_keeps_stocks_and_skips_cash_futures_and_bookkeeping():
    """Cash, the index future and the null-ticker / null-weight lines are not holdings."""
    print("Testing parse_holdings...")
    as_of, holdings = qf.parse_holdings(SAMPLE_RESPONSE)
    assert as_of == "2026-10-30", as_of   # the business date, not the Sunday effectiveDate
    assert holdings == {
        "AAPL": {"name": "Apple Inc", "weight": 8.169369, "shares": 109972726.0},
        "NVDA": {"name": "NVIDIA Corp", "weight": 8.010571, "shares": 181602774.0},
        "ASML": {"name": "ASML Holding NV", "weight": 0.661015, "shares": 1955770.0},
    }, holdings
    print("  OK")


def test_parse_falls_back_to_effective_date():
    print("Testing parse_holdings date fallback...")
    data = {"effectiveDate": "2026-10-30T00:00:00", "holdings": [_stock_row("A", 1.0)]}
    assert qf.parse_holdings(data)[0] == "2026-10-30"
    print("  OK")


def test_parse_unescapes_html_in_names():
    print("Testing parse_holdings names...")
    data = {"effectiveBusinessDate": "2026-10-30",
            "holdings": [_stock_row("T", 1.0, name="AT&amp;T Inc")]}
    assert qf.parse_holdings(data)[1]["T"]["name"] == "AT&T Inc"
    print("  OK")


def test_parse_sums_duplicate_tickers():
    print("Testing parse_holdings duplicates...")
    data = {"effectiveBusinessDate": "2026-10-30",
            "holdings": [_stock_row("A", 1.0, units=10), _stock_row("A", 0.5, units=5)]}
    _, holdings = qf.parse_holdings(data)
    assert holdings["A"]["weight"] == 1.5 and holdings["A"]["shares"] == 15.0, holdings
    print("  OK")


def test_parse_rejects_malformed_responses():
    print("Testing parse_holdings errors...")
    rows = [_stock_row("A", 1.0)]
    _raises(ValueError, qf.parse_holdings, {})
    _raises(ValueError, qf.parse_holdings, {"effectiveBusinessDate": "2026-10-30", "holdings": []})
    _raises(ValueError, qf.parse_holdings, {"holdings": rows})                       # no date
    _raises(ValueError, qf.parse_holdings, {"effectiveBusinessDate": "not a date", "holdings": rows})
    _raises(ValueError, qf.parse_holdings, {"effectiveBusinessDate": "2026-10-30",
                                            "holdings": [_stock_row("A", "abc")]})   # bad weight
    # Only cash and futures: nothing to report on, and saving it would be wrong.
    _raises(ValueError, qf.parse_holdings, {"effectiveBusinessDate": "2026-10-30",
                                            "holdings": SAMPLE_RESPONSE["holdings"][3:]})
    print("  OK")


def test_fetch_retries_then_succeeds():
    print("Testing fetch retry...")
    session = _Session(requests.ConnectionError("boom"), _Resp(SAMPLE_RESPONSE))
    sleeps = []
    as_of, holdings = qf.fetch_holdings(session=session, sleep=sleeps.append)
    assert as_of == "2026-10-30" and "NVDA" in holdings
    assert sleeps == [2.0], sleeps
    url, kwargs = session.calls[1]
    assert len(session.calls) == 2 and url == qf.INVESCO_URL, session.calls
    assert kwargs["params"] == {"idType": "cusip", "productType": "ETF"}, kwargs
    assert "46090E103" in url and kwargs["headers"]["User-Agent"] == qf.USER_AGENT, (url, kwargs)
    print("  OK")


def test_fetch_gives_up_after_three_attempts():
    print("Testing fetch giving up...")
    session = _Session(requests.ConnectionError("down"),
                       _Resp(error=requests.HTTPError("503")),
                       requests.Timeout("slow"))
    sleeps = []
    exc = _raises(qf.HoldingsFetchError, qf.fetch_holdings, session=session, sleep=sleeps.append)
    assert "3 attempts" in str(exc), exc
    assert sleeps == [2.0, 4.0], sleeps
    print("  OK")


class _BadJson(_Resp):
    """A response whose body is not JSON (what a 406 / HTML error page looks like)."""

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_parse_rejects_wrongly_shaped_responses():
    print("Testing parse_holdings shape errors...")
    base = {"effectiveBusinessDate": "2026-10-30"}
    _raises(ValueError, qf.parse_holdings, {**base, "holdings": ["x"]})            # a row that is not an object
    _raises(ValueError, qf.parse_holdings, {**base, "holdings": [None]})
    _raises(ValueError, qf.parse_holdings, {**base, "holdings": {"AAPL": 8.1}})    # an object, not a list
    _raises(ValueError, qf.parse_holdings, {**base, "holdings": "AAPL"})
    _raises(ValueError, qf.parse_holdings, [_stock_row("A", 1.0)])                 # top level is a list
    print("  OK")


def test_parse_rejects_non_finite_numbers():
    """A NaN weight would make every comparison in the diff false and corrupt the snapshot file."""
    print("Testing parse_holdings non-finite numbers...")
    base = {"effectiveBusinessDate": "2026-10-30"}
    for bad in (float("nan"), "nan", "inf", "-inf", True):
        _raises(ValueError, qf.parse_holdings, {**base, "holdings": [_stock_row("A", bad)]})
    _raises(ValueError, qf.parse_holdings, {**base, "holdings": [_stock_row("A", 1.0, units="nan")]})
    print("  OK")


def test_parse_rejects_impossible_dates():
    print("Testing parse_holdings dates...")
    rows = [_stock_row("A", 1.0)]
    for bad in ("2026-13-45", "2026-02-30", "0000-00-00"):
        _raises(ValueError, qf.parse_holdings, {"effectiveBusinessDate": bad, "holdings": rows})
    print("  OK")


def test_fetch_turns_a_malformed_body_into_a_fetch_error():
    """Whatever is wrong with the body, the caller only ever sees HoldingsFetchError."""
    print("Testing fetch with malformed bodies...")
    garbled = {"effectiveBusinessDate": "2026-10-30", "holdings": ["x"]}
    for responses in ((_Resp(garbled),) * 3, (_BadJson(),) * 3):
        sleeps = []
        _raises(qf.HoldingsFetchError, qf.fetch_holdings,
                session=_Session(*responses), sleep=sleeps.append)
        assert sleeps == [2.0, 4.0], sleeps
    print("  OK")


def test_fetch_error_keeps_the_underlying_cause():
    print("Testing fetch error chaining...")
    last = requests.Timeout("slow")
    session = _Session(requests.ConnectionError("a"), requests.ConnectionError("b"), last)
    exc = _raises(qf.HoldingsFetchError, qf.fetch_holdings, session=session, sleep=lambda s: None)
    assert exc.__cause__ is last, exc.__cause__
    print("  OK")


DAY1 = {  # as of 2026-10-29
    "NVDA": _h("NVIDIA Corp", 9.00, 100.0),
    "AAPL": _h("Apple Inc", 8.00, 90.0),
    "OLD": _h("Old Co", 0.50, 5.0),
}
DAY2 = {  # as of 2026-10-30: OLD dropped, NEW added, NVDA +0.2, AAPL flat
    "NVDA": _h("NVIDIA Corp", 9.20, 100.0),
    "AAPL": _h("Apple Inc", 8.00, 90.0),
    "NEW": _h("New Co", 0.40, 4.0),
}


class temp_stock:
    """Point the job at a temp stock/ dir and mock its logger."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "stock"
        self._patches = [
            mock.patch.object(qj, "STOCK_DIR", self.dir),
            mock.patch.object(qj, "write_log"),
        ]
        started = [patch.start() for patch in self._patches]
        self.log = started[1]  # the write_log mock
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False

    def files(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.is_dir() else []


def _fetch_returns(as_of, holdings):
    return mock.patch.object(qj, "fetch_holdings", return_value=(as_of, holdings))


def test_first_run_saves_a_baseline():
    print("Testing first run baseline...")
    with temp_stock() as stock, _fetch_returns("2026-10-29", DAY1):
        res = qj.run({})
        assert res["status"] == "ok" and res["baseline"] is True, res
        assert res["as_of"] == "2026-10-29" and res["previous_as_of"] is None, res
        assert stock.files() == [
            "qqqholdings_2026-10-29.json",
            "qqqholdings_report_2026-10-29.json",
            "qqqholdings_report_2026-10-29.txt",
        ], stock.files()
        assert "Baseline" in res["message"], res["message"]
    print("  OK")


def test_second_run_with_same_as_of_date_writes_nothing():
    """Holiday, or Invesco has not published yet: the as-of date has not moved."""
    print("Testing idempotence...")
    with temp_stock() as stock, _fetch_returns("2026-10-29", DAY1):
        qj.run({})
        before = stock.files()
        res = qj.run({})
        assert res["status"] == "no_new_data" and res["as_of"] == "2026-10-29", res
        assert stock.files() == before, stock.files()
    print("  OK")


def test_next_day_reports_the_differences():
    print("Testing next-day report...")
    with temp_stock() as stock:
        with _fetch_returns("2026-10-29", DAY1):
            qj.run({})
        with _fetch_returns("2026-10-30", DAY2):
            res = qj.run({})
        assert res["status"] == "ok" and res["baseline"] is False, res
        assert res["previous_as_of"] == "2026-10-29", res
        assert (res["added"], res["removed"], res["changed"]) == (1, 1, 1), res
        text = res["message"]
        assert "NEW" in text and "OLD" in text, text
        assert "9.00% -> 9.20%" in text and "+0.20 pp" in text, text
        saved = json.loads((stock.dir / "qqqholdings_report_2026-10-30.json").read_text())
        assert saved["added"][0]["ticker"] == "NEW", saved
        assert saved["removed"][0]["ticker"] == "OLD", saved
        assert saved["changed"][0]["ticker"] == "NVDA", saved
        assert (stock.dir / "qqqholdings_report_2026-10-30.txt").read_text().strip() == text.strip()
    print("  OK")


def test_run_compares_against_the_previous_snapshot_not_yesterday():
    """Monday's run compares against Friday's snapshot."""
    print("Testing weekend gap...")
    with temp_stock():
        with _fetch_returns("2026-10-30", DAY1):  # a Friday
            qj.run({})
        with _fetch_returns("2026-11-02", DAY2):  # the Monday
            res = qj.run({})
        assert res["previous_as_of"] == "2026-10-30", res
    print("  OK")


def test_threshold_can_be_overridden_per_run():
    print("Testing threshold override...")
    nudged = dict(DAY1, NVDA=_h("NVIDIA Corp", 9.05, 100.0))
    with temp_stock():
        with _fetch_returns("2026-10-29", DAY1):
            qj.run({})
        with _fetch_returns("2026-10-30", nudged):
            assert qj.run({})["changed"] == 1  # default 0.01 pp keeps a 0.05 move
    with temp_stock():
        with _fetch_returns("2026-10-29", DAY1):
            qj.run({})
        with _fetch_returns("2026-10-30", nudged):
            assert qj.run({"threshold": "0.1"})["changed"] == 0  # query strings arrive as text
    print("  OK")


def test_fetch_failure_writes_nothing_and_reports_an_error():
    print("Testing fetch failure...")
    with temp_stock() as stock:
        with mock.patch.object(qj, "fetch_holdings",
                               side_effect=qj.HoldingsFetchError("down")):
            res = qj.run({})
        assert res["status"] == "error" and "down" in res["message"], res
        assert stock.files() == [], stock.files()
        assert stock.log.called
    print("  OK")


def test_failed_snapshot_write_is_retried_next_run():
    """The snapshot is written last, so a crash before it leaves no phantom 'done' marker."""
    print("Testing crash safety...")
    real = qj._write_atomic

    def flaky(path, text):
        if path.name == "qqqholdings_2026-10-29.json":
            raise OSError("disk full")
        real(path, text)

    with temp_stock(), _fetch_returns("2026-10-29", DAY1):
        with mock.patch.object(qj, "_write_atomic", side_effect=flaky):
            res = qj.run({})
        assert res["status"] == "error" and "disk full" in res["message"], res
        assert qj._dates(qj.SNAPSHOT_RE) == []
        res = qj.run({})
        assert res["status"] == "ok" and res["baseline"] is True, res
    print("  OK")


def test_read_returns_latest_or_requested_report():
    print("Testing read...")
    with temp_stock():
        empty = qj.read({})
        assert empty["status"] == "not_found", empty
        with _fetch_returns("2026-10-29", DAY1):
            qj.run({})
        with _fetch_returns("2026-10-30", DAY2):
            qj.run({})
        latest = qj.read({})
        assert latest["status"] == "ok" and latest["as_of"] == "2026-10-30", latest
        assert latest["report"]["added"][0]["ticker"] == "NEW", latest
        assert "NEW" in latest["message"]
        older = qj.read({"date": "2026-10-29"})
        assert older["as_of"] == "2026-10-29" and older["report"]["baseline"] is True, older
        assert qj.read({"date": "2020-01-01"})["status"] == "not_found"
    print("  OK")


def test_read_rejects_dates_that_are_not_dates():
    """The date becomes part of a file name; anything but YYYY-MM-DD must be refused."""
    print("Testing read path safety...")
    with temp_stock():
        for bad in ("../state/presence", "2026-10-3", "latest", "2026-10-30/../..", "٢٠٢٦-١٠-٣٠"):
            res = qj.read({"date": bad})
            assert res["status"] == "error", (bad, res)
    print("  OK")


def test_concurrent_runs_write_once_and_neither_fails():
    """The scheduler and a manual webhook call can race: one writes, the others see 'no new data'."""
    print("Testing concurrent runs...")
    real = qj._write_atomic

    def slow(path, text):
        time.sleep(0.05)
        real(path, text)

    results = []
    with temp_stock() as stock, _fetch_returns("2026-10-29", DAY1):
        with mock.patch.object(qj, "_write_atomic", side_effect=slow):
            threads = [threading.Thread(target=lambda: results.append(qj.run({})))
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert sorted(r["status"] for r in results) == ["no_new_data", "no_new_data", "ok"], results
        assert len(stock.files()) == 3, stock.files()   # no leftover temp files
    print("  OK")


def test_unreadable_previous_snapshot_is_reported_not_raised():
    print("Testing a corrupt previous snapshot...")
    for damage in ("{not json", "{}", "[1]"):
        with temp_stock() as stock:
            with _fetch_returns("2026-10-29", DAY1):
                qj.run({})
            (stock.dir / "qqqholdings_2026-10-29.json").write_text(damage)
            with _fetch_returns("2026-10-30", DAY2):
                res = qj.run({})
            assert res["status"] == "error" and "could not save" in res["message"], (damage, res)
            assert "qqqholdings_2026-10-30.json" not in stock.files(), stock.files()
    print("  OK")


def test_bad_thresholds_are_rejected_before_anything_is_fetched():
    """A NaN or negative threshold would give an empty-looking report that then counts as done."""
    print("Testing threshold validation...")
    with temp_stock() as stock, _fetch_returns("2026-10-29", DAY1) as fetch:
        for bad in ("nan", "inf", "-inf", "-1", -0.5, True, "abc", None, [1]):
            res = qj.run({"threshold": bad})
            assert res["status"] == "error" and "threshold" in res["message"], (bad, res)
        assert not fetch.called and stock.files() == []
        assert qj.run({"threshold": "0"})["status"] == "ok"   # zero is allowed: report every move
    print("  OK")


def test_an_older_as_of_date_than_the_latest_snapshot_is_not_written():
    print("Testing an out-of-order date...")
    with temp_stock() as stock:
        with _fetch_returns("2026-10-30", DAY2):
            qj.run({})
        before = stock.files()
        with _fetch_returns("2026-10-29", DAY1):
            res = qj.run({})
        assert res["status"] == "no_new_data" and "2026-10-30" in res["message"], res
        assert stock.files() == before, stock.files()
    print("  OK")


def test_a_logging_failure_does_not_fail_a_finished_run():
    print("Testing logging failure...")
    with temp_stock() as stock, _fetch_returns("2026-10-29", DAY1):
        stock.log.side_effect = RuntimeError("logs/ is read-only")
        res = qj.run({})
        assert res["status"] == "ok", res
        assert len(stock.files()) == 3, stock.files()
        assert qj.run({})["status"] == "no_new_data"   # the run really was recorded
    print("  OK")


def test_read_reports_a_corrupt_report_file():
    print("Testing read of a corrupt report...")
    with temp_stock() as stock:
        with _fetch_returns("2026-10-29", DAY1):
            qj.run({})
        (stock.dir / "qqqholdings_report_2026-10-29.json").write_text("{oops")
        res = qj.read({"date": "2026-10-29"})
        assert res["status"] == "error" and "2026-10-29" in res["message"], res
    print("  OK")


def test_webhooks_are_registered():
    print("Testing webhook registration...")
    config = json.loads((Path(__file__).parent.parent / "configs" / "config.json").read_text())
    hooks = {h["path"]: h for h in config["webhooks"]}
    for path, function in (("/webhook/qqqholdings/run", "run"),
                           ("/webhook/qqqholdings/read", "read")):
        hook = hooks.get(path)
        assert hook, f"{path} not registered in configs/config.json"
        assert hook["module"] == "jobs.qqqholdings_job", hook
        assert hook["function"] == function, hook
        assert hook["require_secret"] is True, hook
        assert hasattr(qj, function), function
    print("  OK")


def test_parse_real_invesco_response():
    """The saved 2026-10-30 response: 106 rows, of which 101 are stocks."""
    print("Testing the real Invesco response...")
    fixture = Path(__file__).parent / "fixtures" / "qqq_cusip_2026-10-30.json"
    as_of, holdings = qf.parse_holdings(json.loads(fixture.read_text(encoding="utf-8")))
    assert as_of == "2026-10-30", as_of
    assert len(holdings) == 101, len(holdings)
    assert abs(sum(h["weight"] for h in holdings.values()) - 99.843) < 0.01
    assert holdings["AAPL"]["weight"] == 8.169369, holdings["AAPL"]
    assert holdings["ASML"]["name"] == "ASML Holding NV", holdings["ASML"]
    skipped = {"USD", "NQZ6", "USDPDV"}
    assert not skipped & set(holdings), skipped & set(holdings)
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} QQQ holdings tests passed!")
