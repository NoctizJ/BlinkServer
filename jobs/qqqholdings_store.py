#!/usr/bin/env python3
"""On-disk storage for the QQQ holdings: one snapshot per as-of date, plus its report.

Everything lives in ``stock/qqqholdings/`` (the date is the as-of date inside
Invesco's file):

    qqqholdings_<date>.json          the day's snapshot: {"as_of", "holdings"}
    qqqholdings_report_<date>.json   the structured report
    qqqholdings_report_<date>.txt    the same report as text

The QQQ job (jobs/qqqholdings_job.py) is the only writer. Anything that needs the
saved holdings reads them through :func:`snapshot_dates` and :func:`load_snapshot`.
"""

import json
import re
from pathlib import Path
from typing import Dict, List

from jobs.atomic_write import write_atomic

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCK_DIR = REPO_ROOT / "stock" / "qqqholdings"

SNAPSHOT_RE = re.compile(r"^qqqholdings_([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$")


def _snapshot_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_{as_of}.json"


def _report_json_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_report_{as_of}.json"


def _report_text_path(as_of: str) -> Path:
    return STOCK_DIR / f"qqqholdings_report_{as_of}.txt"


def snapshot_dates() -> List[str]:
    """The as-of dates that have a saved snapshot, oldest first."""
    if not STOCK_DIR.is_dir():
        return []
    return sorted(m.group(1) for p in STOCK_DIR.iterdir() if (m := SNAPSHOT_RE.match(p.name)))


def load_snapshot(as_of: str) -> Dict[str, dict]:
    """The holdings saved for ``as_of``: ``{ticker: {"name", "weight", "shares"}}``.

    Raises OSError, ValueError, KeyError or TypeError if the file is missing or damaged."""
    return json.loads(_snapshot_path(as_of).read_text(encoding="utf-8"))["holdings"]


def save_day(as_of: str, holdings: Dict[str, dict], report: dict, report_text: str) -> None:
    """Save a day's report files and snapshot.

    The snapshot goes LAST: its existence is what marks the date as done, so a crash
    before it means the next run simply does the work again."""
    write_atomic(_report_json_path(as_of), json.dumps(report, indent=2, ensure_ascii=False))
    write_atomic(_report_text_path(as_of), report_text + "\n")
    write_atomic(_snapshot_path(as_of),
                 json.dumps({"as_of": as_of, "holdings": holdings}, indent=2, ensure_ascii=False))
