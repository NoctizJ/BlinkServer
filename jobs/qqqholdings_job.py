#!/usr/bin/env python3
"""Daily QQQ holdings report.

Two webhooks share this module (see config.json), and the scheduler calls
``run`` every weekday morning (see configs/schedule.json):

  - run(payload)  -> POST /webhook/qqqholdings/run
  - read(payload) -> POST /webhook/qqqholdings/read

``run`` downloads the holdings, compares them with the previous snapshot in
``stock/`` and saves a report of what was added, what was removed and how the
weight of each remaining ticker moved::

    {}                       # the normal call
    {"threshold": 0.05}      # ignore weight moves under 0.05 percentage points

``read`` returns a saved report, as structured data plus a ready-to-display
``message``::

    {}                        # the latest report
    {"date": "2026-10-30"}    # the report for that as-of date

Files in ``stock/`` (the date is the as-of date inside Invesco's file):

    qqqholdings_<date>.json          the day's snapshot
    qqqholdings_report_<date>.json   the structured report
    qqqholdings_report_<date>.txt    the same report as text

"Yesterday" means the latest earlier snapshot on disk, so Monday's run compares
against Friday. If the as-of date matches a snapshot we already have (a market
holiday, or Invesco has not published yet), or is older than the latest one,
nothing is written.

Problems are reported in the return value (``status: "error"``) rather than
raised, so a webhook caller gets JSON and the scheduler can tell a failed run
from a finished one.
"""

import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from jobs.log_engine import log as write_log
from jobs.qqqholdings_diff import DEFAULT_THRESHOLD, diff_holdings
from jobs.qqqholdings_fetch import HoldingsFetchError, fetch_holdings

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCK_DIR = REPO_ROOT / "stock"

LOG_TYPE = "qqqholdings"
SNAPSHOT_RE = re.compile(r"^qqqholdings_([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$")
REPORT_RE = re.compile(r"^qqqholdings_report_([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# A scheduled run and a manual webhook call can overlap; only one may write at a time.
_RUN_LOCK = threading.Lock()


def _snapshot_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_{as_of}.json"


def _report_json_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_report_{as_of}.json"


def _report_text_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_report_{as_of}.txt"


def _write_atomic(path: Path, text: str) -> None:
    """Write via a uniquely named temp file and rename, so a crash never leaves half a file
    and two writers never share a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _dates(pattern: re.Pattern) -> List[str]:
    """Sorted as-of dates of the files in stock/ that match ``pattern``."""
    if not STOCK_DIR.is_dir():
        return []
    return sorted(m.group(1) for p in STOCK_DIR.iterdir() if (m := pattern.match(p.name)))


def _load_snapshot(as_of: str) -> Dict[str, dict]:
    return json.loads(_snapshot_path(as_of).read_text(encoding="utf-8"))["holdings"]


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


def _log(text: str) -> None:
    """Log without letting a logging problem fail or undo a finished run."""
    try:
        write_log(LOG_TYPE, text)
    except Exception:
        logger.exception("could not write the qqqholdings log entry")


def _valid_threshold(value) -> Optional[float]:
    """The threshold as a float, or None unless it is a finite number, zero or more."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _store_report(as_of: str, holdings: Dict[str, dict], threshold: float) -> dict:
    """Diff against the latest earlier snapshot and write the report and snapshot."""
    dates = _dates(SNAPSHOT_RE)
    if dates and as_of <= dates[-1]:
        # The same date again (a market holiday, or Invesco has not published yet), or an
        # older one: either way there is nothing new to report.
        _log(f"QQQ holdings: no new data (fetched {as_of}, latest saved {dates[-1]})")
        return {"status": "no_new_data", "as_of": as_of,
                "message": f"No new data: the latest holdings are still as of {dates[-1]}"}

    previous = dates[-1] if dates else None
    report = {
        "as_of": as_of,
        "previous_as_of": previous,
        "baseline": previous is None,
        "threshold": threshold,
        "holding_count": len(holdings),
        "added": [], "removed": [], "changed": [],
    }
    try:
        if previous is not None:
            report.update(diff_holdings(_load_snapshot(previous), holdings, threshold))
        text = format_report(report)
        _write_atomic(_report_json_path(as_of), json.dumps(report, indent=2, ensure_ascii=False))
        _write_atomic(_report_text_path(as_of), text + "\n")
        # The snapshot goes last: its existence is what marks this date as done, so a
        # crash before this line means the next run simply does the work again.
        _write_atomic(_snapshot_path(as_of),
                      json.dumps({"as_of": as_of, "holdings": holdings}, indent=2,
                                 ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log(f"QQQ holdings {as_of}: could not save the report: {exc}")
        return {"status": "error", "message": f"could not save the QQQ holdings report: {exc}"}

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
    }


def run(payload: Optional[dict] = None) -> dict:
    """Fetch the holdings, diff against the previous snapshot, save the report."""
    payload = payload or {}
    threshold = _valid_threshold(payload.get("threshold", DEFAULT_THRESHOLD))
    if threshold is None:
        return {"status": "error", "message": "threshold must be a number, zero or more"}

    try:
        as_of, holdings = fetch_holdings()
    except HoldingsFetchError as exc:
        _log(f"QQQ holdings fetch failed: {exc}")
        return {"status": "error", "message": str(exc)}

    with _RUN_LOCK:
        return _store_report(as_of, holdings, threshold)


def read(payload: Optional[dict] = None) -> dict:
    """Return a saved report: the latest, or the one for ``date``."""
    payload = payload or {}
    date = str(payload.get("date") or "").strip() or None
    # The date becomes part of a file name, so accept nothing but YYYY-MM-DD.
    if date is not None and not DATE_RE.match(date):
        return {"status": "error", "message": "date must look like YYYY-MM-DD"}

    if date is None:
        reports = _dates(REPORT_RE)
        if not reports:
            return {"status": "not_found",
                    "message": "No QQQ holdings report has been saved yet"}
        date = reports[-1]

    path = _report_json_path(date)
    if not path.is_file():
        return {"status": "not_found", "message": f"No QQQ holdings report for {date}"}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        text = format_report(report)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "message": f"could not read the QQQ holdings report for {date}: {exc}"}
    return {"status": "ok", "as_of": date, "report": report, "message": text}
