#!/usr/bin/env python3
"""Check saved OHLCV history for missing trading days and fetch what is missing.

Run it from the repo root::

    python3 -m jobs.ticker_ohlcv_repair 30d CurrentQQQ
    python3 -m jobs.ticker_ohlcv_repair 2026-01-01..2026-03-31 AAPL,GOOGL --check-only

or call the webhook (secret required)::

    POST /webhook/ticker/ohlcv/repair
    {"range": "30d", "tickers": "CurrentQQQ", "fill": true}

``range`` and ``tickers`` are exactly as for jobs/ticker_ohlcv.py.

A *trading day* is a day in the range on which the reference fund (QQQ) has a bar on
Yahoo: one extra request, so there is no holiday list to keep up to date. A *hole* is a
trading day with no bar in a ticker's file; a ticker with no file is missing every day,
so this also backfills. Unless ``fill`` is false (``--check-only``), each ticker with
holes is fetched once over the span of its holes, merged into its file, and checked
again; holes Yahoo cannot supply (a ticker that was not listed yet, say) stay reported
as still missing.

``status`` is ``ok`` when nothing is missing (or everything was filled), ``partial``
when holes remain, and ``error`` for bad arguments or when the trading days could not
be fetched. The check stops at the last finished trading day: Yahoo's bar for today is partial
until the market closes, so before 17:00 New York time today is not checked (see ``last_final_day``).
"""

import argparse
import datetime
import logging
import sys
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from jobs import ticker_ohlcv, ticker_ohlcv_store as store
from jobs.log_engine import log as write_log

logger = logging.getLogger(__name__)

LOG_TYPE = "ticker_ohlcv_repair"
REFERENCE_SYMBOL = "QQQ"
MAX_DATES_SHOWN = 5
MARKET_TZ = ZoneInfo("America/New_York")
BARS_FINAL_AFTER = datetime.time(17, 0)   # the US market closes at 16:00 ET; Yahoo's day bar is final by 17:00


def _log(text: str) -> None:
    """Log without letting a logging problem fail a finished check."""
    try:
        write_log(LOG_TYPE, text)
    except Exception:
        logger.exception("could not write the ticker_ohlcv_repair log entry")


def _flag(value: Any, name: str) -> bool:
    """A true/false setting from JSON (a bool) or a query string (``true``, ``0``, ...)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def last_final_day(now: Optional[datetime.datetime] = None) -> datetime.date:
    """The newest day whose Yahoo bar is final: today after 17:00 New York time, else yesterday."""
    now = now.astimezone(MARKET_TZ) if now else datetime.datetime.now(MARKET_TZ)
    return now.date() if now.time() >= BARS_FINAL_AFTER else now.date() - datetime.timedelta(days=1)


def trading_days(start: datetime.date, end: datetime.date) -> List[str]:
    """The dates in start..end on which the reference fund has a bar, oldest first."""
    days = [bar["date"] for bar in ticker_ohlcv.fetch_bars(REFERENCE_SYMBOL, start, end)]
    if not days:
        raise ValueError(f"{REFERENCE_SYMBOL} has no bars for {start} .. {end}")
    return days


def find_holes(symbol: str, days: List[str]) -> List[str]:
    """The ``days`` for which ``symbol`` has no saved bar."""
    saved = {record["date"] for record in store.load_records(symbol)}
    return [day for day in days if day not in saved]


def repair_ticker(symbol: str, days: List[str], fill: bool) -> dict:
    """Check one ticker; unless ``fill`` is false, fetch its holes and check again.

    Returns ``{"ticker", "status": "complete"}``; ``{"ticker", "status": "holes",
    "missing", "filled", "still_missing"}`` (plus a ``reason`` if the fetch failed);
    or ``{"ticker", "status": "failed", "reason"}`` if the ticker could not be checked."""
    try:
        missing = find_holes(symbol, days)
        if not missing:
            return {"ticker": symbol, "status": "complete"}
        result = {"ticker": symbol, "status": "holes", "missing": missing,
                  "filled": [], "still_missing": missing}
        if not fill:
            return result
        fetched = ticker_ohlcv.fetch_ticker(symbol, datetime.date.fromisoformat(missing[0]),
                                            datetime.date.fromisoformat(missing[-1]))
        if fetched["status"] == "failed":
            result["reason"] = fetched["reason"]
            return result
        still = set(find_holes(symbol, days))
        result["filled"] = [day for day in missing if day not in still]
        result["still_missing"] = [day for day in missing if day in still]
        return result
    except Exception as exc:  # one ticker (a damaged file, say) must not stop the others
        return {"ticker": symbol, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def _dates_text(dates: List[str]) -> str:
    shown = ", ".join(dates[:MAX_DATES_SHOWN])
    return shown + (f" (+{len(dates) - MAX_DATES_SHOWN} more)" if len(dates) > MAX_DATES_SHOWN else "")


def format_results(days: List[str], results: List[dict], fill: bool) -> str:
    """One line per ticker that has a problem, in the order requested, then a summary line."""
    lines = []
    for r in results:
        if r["status"] == "holes":
            line = f"{r['ticker']:<8} {len(r['missing'])} missing ({_dates_text(r['missing'])})"
            if not fill:
                line += " -> not filled (check only)"
            else:
                parts = []
                if r["filled"]:
                    parts.append(f"filled {len(r['filled'])}")
                if r["still_missing"]:
                    parts.append(f"still missing {len(r['still_missing'])} "
                                 f"({_dates_text(r['still_missing'])})")
                line += " -> " + ", ".join(parts)
                if "reason" in r:
                    line += f"; fetch failed: {r['reason']}"
            lines.append(line)
        elif r["status"] == "failed":
            lines.append(f"{r['ticker']:<8} FAILED  {r['reason']}")

    holes = [r for r in results if r["status"] == "holes"]
    summary = (f"Checked {len(results)} tickers over {len(days)} trading days "
               f"({days[0]} .. {days[-1]}): {sum(r['status'] == 'complete' for r in results)} complete")
    if holes:
        summary += (f", {len(holes)} with holes ({sum(len(r['missing']) for r in holes)} missing, "
                    f"{sum(len(r['filled']) for r in holes)} filled, "
                    f"{sum(len(r['still_missing']) for r in holes)} still missing)")
    failed = sum(r["status"] == "failed" for r in results)
    if failed:
        summary += f", {failed} could not be checked"
    return "\n".join(lines + [summary])


def run(payload: Optional[dict] = None, now: Optional[datetime.datetime] = None) -> dict:
    """Check ``payload["tickers"]`` over ``payload["range"]`` and (unless ``fill`` is false) fill the holes.

    ``now`` is for tests; the range never extends past :func:`last_final_day`."""
    payload = payload or {}
    try:
        start, end, symbols = ticker_ohlcv.parse_request(payload)
        fill = _flag(payload.get("fill", True), "fill")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    final = last_final_day(now)
    clamped = end > final
    end = min(end, final)
    if end < start:
        return {"status": "error",
                "message": f"nothing to check yet: the range ends after the last finished trading day ({final})"}
    try:
        days = trading_days(start, end)
    except Exception as exc:  # no network, a rate limit, a range with no trading days...
        return {"status": "error",
                "message": f"could not get the trading days from {REFERENCE_SYMBOL}: {exc}"}

    results = [repair_ticker(symbol, days, fill) for symbol in symbols]
    unresolved = any(r["status"] == "failed" or (r["status"] == "holes" and r["still_missing"])
                     for r in results)
    message = format_results(days, results, fill)
    _log(message.splitlines()[-1])
    if clamped:
        message += f"\n(today's bar is not final before 17:00 New York time, so the check stopped at {end})"
    return {
        "status": "partial" if unresolved else "ok",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trading_days": days,
        "results": results,
        "message": message,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """The command line: ``python3 -m jobs.ticker_ohlcv_repair RANGE TICKERS [--check-only]``."""
    parser = argparse.ArgumentParser(
        prog="python3 -m jobs.ticker_ohlcv_repair",
        description="Report trading days missing from the saved OHLCV files and fetch them.")
    parser.add_argument("range", help=ticker_ohlcv.RANGE_HELP)
    parser.add_argument("tickers", nargs="+", help=ticker_ohlcv.TICKERS_HELP)
    parser.add_argument("--check-only", action="store_true",
                        help="report the holes but do not fetch anything to fill them")
    args = parser.parse_args(argv)
    result = run({"range": args.range, "tickers": args.tickers, "fill": not args.check_only})
    print(result["message"])
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
