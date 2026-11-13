#!/usr/bin/env python3
"""On-disk storage for daily OHLCV history: one JSON file per ticker in ``stock/ohlcv/``.

``stock/ohlcv/<SYMBOL>.json`` is a list of ``{"date", "open", "high", "low", "close",
"volume"}`` sorted by date. Saving MERGES by date: new days are added and days saved
again are refreshed, so saving a short range never drops earlier history.
"""

import json
import re
import threading
from pathlib import Path
from typing import List, Tuple

from jobs.atomic_write import write_atomic

REPO_ROOT = Path(__file__).resolve().parent.parent
OHLCV_DIR = REPO_ROOT / "stock" / "ohlcv"

# The symbol is also the file name, so nothing path-like gets through.
SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,14}$")

# Only one save at a time may read, merge and write a ticker file.
_LOCK = threading.Lock()


def merge_records(existing: List[dict], new: List[dict]) -> List[dict]:
    """Both lists by date, a date present in both taking the ``new`` values, sorted by date."""
    by_date = {record["date"]: record for record in existing}
    by_date.update({record["date"]: record for record in new})
    return [by_date[date] for date in sorted(by_date)]


def _path(symbol: str) -> Path:
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"not a valid ticker symbol: {symbol!r}")
    return OHLCV_DIR / f"{symbol}.json"


def _read(path: Path) -> List[dict]:
    """The records in an (already validated) file path; [] if there is no file."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(r, dict) and "date" in r for r in data):
        raise ValueError(f"{path.name} is not a list of daily records")
    return data


def load_records(symbol: str) -> List[dict]:
    """A ticker's saved records, oldest first ([] if it has no file).

    Raises ValueError if the symbol is not valid or the file is not a list of daily
    records, and OSError if the file cannot be read."""
    return _read(_path(symbol))


def save_merged(symbol: str, records: List[dict]) -> Tuple[int, List[dict]]:
    """Merge ``records`` into the ticker's file; returns (days it had before, all records now).

    Reading, merging and writing happen under one lock, so saves racing on a file
    never lose a day."""
    path = _path(symbol)
    with _LOCK:
        existing = _read(path)
        merged = merge_records(existing, records)
        write_atomic(path, json.dumps(merged, indent=2))
    return len(existing), merged
