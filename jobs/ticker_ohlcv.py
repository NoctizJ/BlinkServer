#!/usr/bin/env python3
"""Fetch daily OHLCV price history for tickers and keep it, one JSON file per ticker.

Run it from the repo root::

    python3 -m jobs.ticker_ohlcv 30d AAPL,GOOGL
    python3 -m jobs.ticker_ohlcv 2026-01-01..2026-03-31 CurrentQQQ

or call the webhook (secret required), with the same two things::

    POST /webhook/ticker/ohlcv/fetch
    {"range": "30d", "tickers": "CurrentQQQ"}
    {"range": "2026-01-01..2026-03-31", "tickers": ["AAPL", "GOOGL"]}

``range`` is ``30d`` (or ``30``): from 30 days ago through today; ``START..END``:
those dates, both included; or ``START..``: from START through today.

``tickers`` is a comma list (``AAPL,GOOGL``, ``[AAPL, GOOGL]``, or a JSON list), or
``CurrentQQQ``: every ticker in the newest saved QQQ holdings snapshot (see
jobs/qqqholdings_job.py).

Each ticker's bars go to ``stock/ohlcv/<SYMBOL>.json``: a list of
``{"date", "open", "high", "low", "close", "volume"}`` sorted by date. Fetching a
ticker that already has a file MERGES by date: new days are added and days fetched
again are refreshed, so a short fetch never drops earlier history. The bars are Yahoo
Finance's daily bars with ``auto_adjust=False`` (not dividend-adjusted); a day with a
missing price is skipped.

One bad ticker never stops the others: it is reported as failed, and fetching it
again later is safe because of the merge. The result ``status`` is ``ok`` (every
ticker saved), ``partial`` (some failed) or ``error`` (none saved, or bad arguments).
"""

import argparse
import datetime
import logging
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from jobs import qqqholdings_job, scheduler, ticker_ohlcv_store as store
from jobs.log_engine import log as write_log

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = REPO_ROOT / "configs" / "ticker_ohlcv_schedule.json"

LOG_TYPE = "ticker_ohlcv"
CURRENT_QQQ = "currentqqq"  # compared in lower case
DAYS_RE = re.compile(r"^([0-9]+)d?$", re.IGNORECASE)
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DAILY_LOOKBACK_DAYS = 7
MAX_FAILURES_LOGGED = 10

# The command-line help for the two arguments, shared with jobs/ticker_ohlcv_repair.py.
RANGE_HELP = ('"30d" = the last 30 days up to today, or START..END (YYYY-MM-DD, both included); '
              'START.. runs through today')
TICKERS_HELP = ("symbols such as AAPL,GOOGL (or AAPL GOOGL), or CurrentQQQ for "
                "every ticker in the newest saved QQQ holdings list")


def _log(text: str) -> None:
    """Log without letting a logging problem fail a finished fetch."""
    try:
        write_log(LOG_TYPE, text)
    except Exception:
        logger.exception("could not write the ticker_ohlcv log entry")


def parse_range(spec: Any, today: Optional[datetime.date] = None
                ) -> Tuple[datetime.date, datetime.date]:
    """Turn ``30d`` / ``START..END`` / ``START..`` into an inclusive (start, end); ValueError if bad."""
    today = today or datetime.date.today()
    text = str(spec or "").strip()
    if not text:
        raise ValueError('range is required: "30d" or "START..END" (YYYY-MM-DD)')
    days = DAYS_RE.match(text)
    if days:
        count = int(days.group(1))
        if count < 1:
            raise ValueError("range must be at least 1 day")
        return today - datetime.timedelta(days=count), today
    first, dots, last = text.partition("..")
    first, last = first.strip(), last.strip()
    if not dots or not DATE_RE.match(first) or (last and not DATE_RE.match(last)):
        raise ValueError(f'range must look like "30d" or "START..END" (YYYY-MM-DD), got {text!r}')
    try:
        start = datetime.date.fromisoformat(first)
        end = datetime.date.fromisoformat(last) if last else today
    except ValueError:
        raise ValueError(f"range has a date that does not exist: {text!r}") from None
    if start > end:
        raise ValueError(f"range starts after it ends: {start} .. {end}")
    if start > today:
        raise ValueError(f"range starts in the future: {start}")
    return start, end


def _current_qqq_tickers() -> List[str]:
    """Every ticker in the newest saved QQQ holdings snapshot, largest weight first."""
    result = qqqholdings_job.read({})
    if result["status"] != "ok":
        raise ValueError(f"CurrentQQQ needs a saved QQQ holdings list ({result['message']}); "
                         "run /webhook/qqqholdings/run first")
    tickers = [row["ticker"] for row in result["holdings"]]
    if not tickers:
        raise ValueError("the saved QQQ holdings list is empty")
    return tickers


def resolve_tickers(spec: Any) -> List[str]:
    """A ticker list, or ``CurrentQQQ``, as unique upper-case symbols in order; ValueError if empty."""
    if isinstance(spec, (list, tuple)):
        spec = ",".join(str(item) for item in spec)
    text = str(spec or "").strip()
    if text.lower() == CURRENT_QQQ:
        symbols = _current_qqq_tickers()
    else:
        symbols = [s for s in re.split(r"[\s,]+", re.sub(r"[\[\]'\"]", " ", text)) if s]
    unique = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    if not unique:
        raise ValueError("tickers is required: a list like AAPL,GOOGL, or CurrentQQQ")
    return unique


def _download(symbol: str, start: datetime.date, end: datetime.date):
    """Daily bars from Yahoo as a pandas DataFrame (the only code that touches yfinance).

    yfinance treats ``end`` as exclusive, so one day is added to include it.
    ``raise_errors=True`` makes a network failure or a rate limit show up as the ticker's
    failure reason; by default yfinance hides them and returns an empty frame, which would
    look like an unknown symbol."""
    import yfinance as yf

    return yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + datetime.timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        raise_errors=True,
    )


def to_records(df) -> List[dict]:
    """Daily records from a yfinance history frame; rows with a missing price are dropped."""
    if df is None or len(df) == 0:
        return []
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return [{
        "date": date.strftime("%Y-%m-%d"),
        "open": round(float(row["Open"]), 4),
        "high": round(float(row["High"]), 4),
        "low": round(float(row["Low"]), 4),
        "close": round(float(row["Close"]), 4),
        "volume": int(row["Volume"]),
    } for date, row in df.iterrows()]


def fetch_bars(symbol: str, start: datetime.date, end: datetime.date) -> List[dict]:
    """Daily records for symbol over start..end (end included), straight from Yahoo."""
    return to_records(_download(symbol, start, end))


def _failed(symbol: str, reason: str) -> dict:
    return {"ticker": symbol, "status": "failed", "reason": reason}


def fetch_ticker(symbol: str, start: datetime.date, end: datetime.date) -> dict:
    """Fetch one ticker and merge it into its file; returns a ``saved`` or ``failed`` result."""
    if not store.SYMBOL_RE.match(symbol):
        return _failed(symbol, "not a valid ticker symbol")
    try:
        records = fetch_bars(symbol, start, end)
        if not records:
            return _failed(symbol, f"no data for {start} .. {end} (no trading days in the range)")
        existing_count, merged = store.save_merged(symbol, records)
    except Exception as exc:  # one ticker failing, for any reason, must not stop the others
        return _failed(symbol, f"{type(exc).__name__}: {exc}")
    return {"ticker": symbol, "status": "saved", "fetched": len(records),
            "added": len(merged) - existing_count, "total": len(merged),
            "first": merged[0]["date"], "last": merged[-1]["date"]}


def format_results(start: datetime.date, end: datetime.date, results: List[dict]) -> str:
    """One line per ticker, in the order requested, then a summary line."""
    lines = []
    for r in results:
        if r["status"] == "saved":
            lines.append(f"{r['ticker']:<8} saved   {r['fetched']} fetched, {r['added']} new, "
                         f"{r['total']} in file ({r['first']} .. {r['last']})")
        else:
            lines.append(f"{r['ticker']:<8} FAILED  {r['reason']}")
    saved = sum(1 for r in results if r["status"] == "saved")
    failed = len(results) - saved
    lines.append(f"OHLCV {start} .. {end}: {saved} of {len(results)} saved"
                 + (f", {failed} failed" if failed else ""))
    return "\n".join(lines)


def parse_request(payload: dict) -> Tuple[datetime.date, datetime.date, List[str]]:
    """The (start, end, symbols) a fetch or repair request asks for; ValueError if the request is bad."""
    start, end = parse_range(payload.get("range"))
    symbols = resolve_tickers(payload.get("tickers"))
    return start, end, symbols


def fetch_many(start: datetime.date, end: datetime.date, symbols: List[str]) -> dict:
    """Fetch every symbol over start..end and report it (the result dict ``run`` has always returned)."""
    results = [fetch_ticker(symbol, start, end) for symbol in symbols]
    saved = [r for r in results if r["status"] == "saved"]
    failed = [r for r in results if r["status"] == "failed"]
    message = format_results(start, end, results)
    _log(message.splitlines()[-1])
    if failed:
        text = "failed: " + "; ".join(f"{r['ticker']} ({r['reason']})" for r in failed[:MAX_FAILURES_LOGGED])
        if len(failed) > MAX_FAILURES_LOGGED:
            text += f"; +{len(failed) - MAX_FAILURES_LOGGED} more"
        _log(text)
    return {
        "status": "ok" if not failed else ("partial" if saved else "error"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "requested": len(symbols),
        "saved": saved,
        "failed": failed,
        "message": message,
    }


def run(payload: Optional[dict] = None) -> dict:
    """Fetch ``payload["range"]`` of daily history for ``payload["tickers"]`` and save it."""
    try:
        start, end, symbols = parse_request(payload or {})
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    return fetch_many(start, end, symbols)


def _schedule_today(now: Optional[datetime.datetime] = None) -> datetime.date:
    """Today's date in the daily schedule's time zone: the same day the scheduler records the run under."""
    schedule = scheduler.load_schedule(SCHEDULE_PATH)
    tz = schedule.tz if schedule else None
    return (now.astimezone(tz) if now else datetime.datetime.now(tz)).date()


def run_daily(payload: Optional[dict] = None, today: Optional[datetime.date] = None) -> dict:
    """The scheduled job: the last DAILY_LOOKBACK_DAYS days of bars (today included) for every CurrentQQQ ticker.

    The look-back makes every run heal the last week: a ticker that failed yesterday, a day the Mac slept through,
    or a day Yahoo had not published yet is filled by the next run (the merge makes re-fetching harmless). A run that
    saved nothing because every ticker failed is an ``error`` and the scheduler retries it; otherwise it is done."""
    today = today or _schedule_today()
    try:
        symbols = resolve_tickers(CURRENT_QQQ)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    return fetch_many(today - datetime.timedelta(days=DAILY_LOOKBACK_DAYS), today, symbols)


def main(argv: Optional[List[str]] = None) -> int:
    """The command line: ``python3 -m jobs.ticker_ohlcv RANGE TICKERS``. Exit 0 only if all saved."""
    parser = argparse.ArgumentParser(
        prog="python3 -m jobs.ticker_ohlcv",
        description="Fetch daily OHLCV history and save it to stock/ohlcv/<SYMBOL>.json "
                    "(merged by date with what is already there).")
    parser.add_argument("range", help=RANGE_HELP)
    parser.add_argument("tickers", nargs="+", help=TICKERS_HELP)
    args = parser.parse_args(argv)
    result = run({"range": args.range, "tickers": args.tickers})
    print(result["message"])
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
