#!/usr/bin/env python3
"""Daily QQQ holdings report.

Three webhooks share this module (see config.json), and the scheduler calls
``run`` every weekday morning (see configs/schedule.json):

  - run(payload)  -> POST /webhook/qqqholdings/run
  - read(payload) -> POST /webhook/qqqholdings/read
  - diff(payload) -> POST /webhook/qqqholdings/diff

``run`` downloads the holdings, compares them with the previous snapshot in
``stock/`` and saves a report of what was added, what was removed and how the
weight of each remaining ticker moved::

    {}                       # the normal call
    {"threshold": 0.05}      # ignore weight moves under 0.05 percentage points

When a run finds new data in which a ticker was added or removed, it also sends a phone
notification (best effort; see jobs/qqqholdings_notify.py), reported as ``notification``
in the result.

``read`` returns the holdings of one snapshot, largest weight first, each with its rank::

    {}                        # the newest snapshot
    {"date": "2026-10-30"}    # the snapshot for that as-of date

``diff`` compares two snapshots and returns what was added, removed and re-weighted::

    {}                        # the newest snapshot against the one before it
    {"first_date": "2026-10-28", "second_date": "2026-10-30"}   # from first to second
    {"threshold": 0.05}       # optional: ignore weight moves under 0.05 percentage points

The files this job saves, and how they are read back, are in ``jobs/qqqholdings_store.py``.

"Yesterday" means the latest earlier snapshot on disk, so Monday's run compares
against Friday. If the as-of date matches a snapshot we already have (a market
holiday, or Invesco has not published yet), or is older than the latest one,
nothing is written.

Problems are reported in the return value (``status: "error"``) rather than
raised, so a webhook caller gets JSON and the scheduler can tell a failed run
from a finished one.
"""

import logging
import math
import re
import threading
from typing import Callable, Dict, List, Optional, Tuple

from jobs import qqqholdings_store as store
from jobs.log_engine import log as write_log
from jobs.qqqholdings_diff import DEFAULT_THRESHOLD, diff_holdings
from jobs.qqqholdings_fetch import HoldingsFetchError, fetch_holdings
from jobs.qqqholdings_notify import notify_changes

logger = logging.getLogger(__name__)

LOG_TYPE = "qqqholdings"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# A snapshot that cannot be used: a missing or unreadable file (OSError), bad JSON (ValueError),
# `{}` with no "holdings" (KeyError) or `[1]` (TypeError).
_BAD_SNAPSHOT = (OSError, ValueError, KeyError, TypeError)

# A scheduled run and a manual webhook call can overlap; only one may write at a time.
_RUN_LOCK = threading.Lock()


def _section(title: str, rows: List[dict], line: Callable[[dict], str]) -> List[str]:
    """A titled block with one line per row, or '  none'."""
    return [f"{title} ({len(rows)})", *([line(r) for r in rows] or ["  none"])]


def format_report(report: dict) -> str:
    """Render a report dict as plain text."""
    if report["baseline"]:
        return (f"QQQ holdings — {report['as_of']}\n"
                f"Baseline saved: {report['holding_count']} holdings. "
                f"No earlier snapshot to compare against.")

    lines = [f"QQQ holdings — {report['as_of']} (vs {report['previous_as_of']})"]
    if not (report["added"] or report["removed"] or report["changed"]):
        lines.append("No changes.")
        return "\n".join(lines)

    lines += _section("Added", report["added"],
                      lambda r: f"  {r['ticker']:<6} {r['name']}  {r['weight']:.2f}%")
    lines += _section("Removed", report["removed"],
                      lambda r: f"  {r['ticker']:<6} {r['name']}  was {r['weight']:.2f}%")
    lines += _section("Weight changes", report["changed"],
                      lambda r: f"  {r['ticker']:<6} {r['old_weight']:.2f}% -> {r['new_weight']:.2f}%  "
                                f"({r['change']:+.2f} pp)")
    return "\n".join(lines)


def ranked_holdings(holdings: Dict[str, dict]) -> List[dict]:
    """Holdings largest weight first (ties by ticker), each with its rank from 1."""
    rows = sorted(holdings.items(), key=lambda item: (-item[1]["weight"], item[0]))
    return [{"rank": rank, "ticker": ticker, "name": h["name"],
             "weight": h["weight"], "shares": h["shares"]}
            for rank, (ticker, h) in enumerate(rows, 1)]


def format_holdings(as_of: str, ranked: List[dict], total_weight: float) -> str:
    """Render ranked holdings as plain text."""
    width = len(str(len(ranked)))
    lines = [f"QQQ holdings — {as_of} ({len(ranked)} holdings, {total_weight:.2f}% of the fund)"]
    lines += [f"  {r['rank']:>{width}}  {r['ticker']:<6} {r['name']}  {r['weight']:.2f}%"
              for r in ranked]
    return "\n".join(lines)


def _date_param(payload: dict, key: str) -> Optional[str]:
    """A YYYY-MM-DD date from the payload, or None when absent; ValueError if malformed.

    The date becomes part of a file name, so nothing but YYYY-MM-DD is accepted."""
    value = str(payload.get(key) or "").strip()
    if value and not DATE_RE.match(value):
        raise ValueError(f"{key} must look like YYYY-MM-DD")
    return value or None


def _not_found(date: Optional[str], dates: List[str]) -> dict:
    if not dates:
        return {"status": "not_found", "message": "No QQQ holdings have been saved yet"}
    return {"status": "not_found",
            "message": f"No QQQ holdings were saved for {date}; "
                       f"saved dates run from {dates[0]} to {dates[-1]}"}


def _log(text: str) -> None:
    """Log without letting a logging problem fail or undo a finished run."""
    try:
        write_log(LOG_TYPE, text)
    except Exception:
        logger.exception("could not write the qqqholdings log entry")


def _threshold_param(payload: dict) -> float:
    """The threshold from the payload as a float; ValueError unless it is a finite number, zero or more."""
    value = payload.get("threshold", DEFAULT_THRESHOLD)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if isinstance(value, bool) or not (math.isfinite(number) and number >= 0):
        raise ValueError("threshold must be a number, zero or more")
    return number


def _make_report(as_of: str, previous: Optional[str], holdings: Dict[str, dict],
                 old_holdings: Optional[Dict[str, dict]], threshold: float) -> dict:
    """The report for ``holdings`` as of ``as_of`` against ``old_holdings`` (None: a baseline)."""
    report = {
        "as_of": as_of,
        "previous_as_of": previous,
        "baseline": old_holdings is None,
        "threshold": threshold,
        "holding_count": len(holdings),
        "added": [], "removed": [], "changed": [],
    }
    if old_holdings is not None:
        report.update(diff_holdings(old_holdings, holdings, threshold))
    return report


def _store_report(as_of: str, holdings: Dict[str, dict],
                  threshold: float) -> Tuple[dict, Optional[dict]]:
    """Diff against the latest earlier snapshot and write the report and snapshot.

    Returns ``(result, report)``: ``report`` is the saved report dict, or None when
    nothing was saved (``no_new_data`` or an error)."""
    dates = store.snapshot_dates()
    if dates and as_of <= dates[-1]:
        # The same date again (a market holiday, or Invesco has not published yet), or an
        # older one: either way there is nothing new to report.
        _log(f"QQQ holdings: no new data (fetched {as_of}, latest saved {dates[-1]})")
        return ({"status": "no_new_data", "as_of": as_of,
                 "message": f"No new data: the latest holdings are still as of {dates[-1]}"}, None)

    previous = dates[-1] if dates else None
    try:
        old = store.load_snapshot(previous) if previous else None
        report = _make_report(as_of, previous, holdings, old, threshold)
        text = format_report(report)
        store.save_day(as_of, holdings, report, text)
    except _BAD_SNAPSHOT as exc:
        _log(f"QQQ holdings {as_of}: could not save the report: {exc}")
        return {"status": "error", "message": f"could not save the QQQ holdings report: {exc}"}, None

    _log(f"QQQ holdings {as_of}: baseline of {len(holdings)} saved" if report["baseline"] else
         f"QQQ holdings {as_of}: +{len(report['added'])} "
         f"-{len(report['removed'])} ~{len(report['changed'])} vs {previous}")
    return {
        "status": "ok",
        "as_of": as_of,
        "previous_as_of": previous,
        "baseline": report["baseline"],
        "added": len(report["added"]),
        "removed": len(report["removed"]),
        "changed": len(report["changed"]),
        "message": text,
    }, report


def _notify(report: dict) -> dict:
    """Tell the phone which tickers were added or removed; best effort, the outcome is logged."""
    try:
        result = notify_changes(report)
    except Exception as exc:  # notify_changes should not raise; a bug in it must not fail a saved run
        result = {"status": "error", "message": str(exc)}
    _log(f"QQQ holdings {report['as_of']}: notification {result.get('status')}: {result.get('message')}")
    return {"status": result.get("status"), "message": result.get("message")}


def run(payload: Optional[dict] = None) -> dict:
    """Fetch the holdings, diff against the previous snapshot, save the report."""
    payload = payload or {}
    try:
        threshold = _threshold_param(payload)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    try:
        as_of, holdings = fetch_holdings()
    except HoldingsFetchError as exc:
        _log(f"QQQ holdings fetch failed: {exc}")
        return {"status": "error", "message": str(exc)}

    with _RUN_LOCK:
        result, report = _store_report(as_of, holdings, threshold)
    # Outside the lock: Home Assistant can take 30 s per device to time out, and the report
    # and snapshot are already saved, so nothing here can block or undo them.
    if report is not None and (report["added"] or report["removed"]):
        result["notification"] = _notify(report)
    return result


def read(payload: Optional[dict] = None) -> dict:
    """Return one snapshot's holdings ranked by weight: the newest, or the one for ``date``."""
    payload = payload or {}
    try:
        date = _date_param(payload, "date")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    dates = store.snapshot_dates()
    date = date or (dates[-1] if dates else None)
    if date not in dates:
        return _not_found(date, dates)
    try:
        ranked = ranked_holdings(store.load_snapshot(date))
        total = round(sum(r["weight"] for r in ranked), 4)
        text = format_holdings(date, ranked, total)
    except _BAD_SNAPSHOT as exc:
        return {"status": "error", "message": f"could not read the QQQ holdings for {date}: {exc}"}
    return {"status": "ok", "as_of": date, "count": len(ranked), "total_weight": total,
            "holdings": ranked, "message": text}


def diff(payload: Optional[dict] = None) -> dict:
    """Diff two snapshots: the newest against the one before it, or ``first_date`` against ``second_date``."""
    payload = payload or {}
    try:
        threshold = _threshold_param(payload)
        first, second = _date_param(payload, "first_date"), _date_param(payload, "second_date")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if bool(first) != bool(second):
        return {"status": "error",
                "message": "give both first_date and second_date, or neither "
                           "(neither compares the two newest snapshots)"}
    dates = store.snapshot_dates()
    if not first:
        if len(dates) < 2:
            return {"status": "not_found",
                    "message": f"Need at least two saved snapshots to compare; found {len(dates)}"}
        first, second = dates[-2], dates[-1]
    for date in (first, second):
        if date not in dates:
            return _not_found(date, dates)
    try:
        report = _make_report(second, first, store.load_snapshot(second), store.load_snapshot(first), threshold)
        text = format_report(report)
    except _BAD_SNAPSHOT as exc:
        return {"status": "error", "message": f"could not compare the QQQ holdings: {exc}"}
    return {"status": "ok", "as_of": second, "previous_as_of": first, "report": report, "message": text}
