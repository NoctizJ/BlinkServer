#!/usr/bin/env python3
"""Location log store for Blink Server.

Persists logged positions **per person, in their own file**, so one person's
history never sits in another's (or in the shared text logs). Each id gets
``state/<id>_loc.json``:

    {
      "id": "娜",
      "entries": [
        {
          "latitude": 37.334606,
          "longitude": -122.009102,
          "address": "Apple Park, Cupertino",
          "time": "2026-08-18 09:15:23.123",
          "recorded_at": "2026-08-18 09:15:23.980",
          "trigger": "arrived home"
        }
      ],
      "last_modified": "2026-08-18 09:15:23.980"
    }

``time`` is the caller's own timestamp, stored verbatim (never reformatted);
``recorded_at`` is when this server wrote the entry; ``trigger`` is the caller's
own reason for logging it (null when they did not say). Entries are appended, so
the newest is last and "latest" means *most recently logged* — not the largest
``time``. Each file keeps at most :data:`MAX_ENTRIES` entries; older ones are
dropped so a chatty phone cannot grow the file without bound.

The identity comes from the ``id`` field of a webhook payload and is resolved by
``presence_state.resolve_person``, so ids mean the same thing here as they do
for presence (a post with no ``id`` is attributed to ``娜``).

Usage:
    from jobs.location_state import append_location, latest_location

    append_location("娜", 37.334606, -122.009102, address="Apple Park",
                    trigger="arrived home")
    latest_location("娜")     # -> {"latitude": 37.334606, ...} or None

Reading a caller's payload (coordinates, ``n``, ``days``) and rendering it
(map links, the history table) belong to :mod:`jobs.location_webhook`; this
module only persists.
"""

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.presence_state import resolve_person
except ImportError:  # pragma: no cover - allows running this file directly
    from presence_state import resolve_person

logger = logging.getLogger(__name__)

# The repo root is the parent of the jobs/ folder that holds this module.
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"

# Every location file is named "<id>_loc.json".
FILE_SUFFIX = "_loc.json"

# Newest-last history cap, per id (~75 KB per file at this size).
MAX_ENTRIES = 500

# Characters that must never reach a filename: path separators, Windows-illegal
# characters, and control bytes. Replaced with "_" (see :func:`_safe_stem`).
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Filename stems are truncated to this many characters, leaving room for the
# suffix inside the 255-byte limit even when the id is non-ASCII.
_MAX_STEM = 64

# Stem used when an id sanitizes down to nothing (e.g. an id of "..").
_FALLBACK_STEM = "unknown"

# The timestamp format this server writes (shared with the logging engine).
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# Other spellings accepted when reading a stored timestamp back, so a hand-edited
# file or a caller's own `time` can still be aged for purging.
_FALLBACK_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _now() -> str:
    """Timestamp in the same format the logging engine uses."""
    return datetime.datetime.now().strftime(TIMESTAMP_FORMAT)[:-3]


def _safe_stem(person: str) -> str:
    """Turn a person's id into a filename stem that cannot escape ``state/``.

    Path separators and control characters become ``_``; leading/trailing dots
    and whitespace are stripped (so ``".."`` cannot become a directory hop);
    non-ASCII names like ``娜`` are kept as-is, since they are perfectly good
    filenames. Two ids that differ only in unsafe characters can collapse to the
    same stem and would then share a file — use plain names to avoid that.
    """
    stem = _UNSAFE_CHARS.sub("_", str(person)).strip().strip(".").strip()
    return (stem[:_MAX_STEM] or _FALLBACK_STEM)


def location_file(person: str) -> Path:
    """Return the path of a person's location file (``state/<id>_loc.json``).

    Reads the current ``STATE_DIR`` at call time so tests can redirect it.
    """
    path = STATE_DIR / f"{_safe_stem(person)}{FILE_SUFFIX}"
    # Belt and braces: an id from an HTTP payload must never write outside
    # state/, whatever _safe_stem let through.
    if path.parent.resolve() != STATE_DIR.resolve():
        raise ValueError(f"Refusing to write outside {STATE_DIR}: {person!r}")
    return path


def _empty_store(person: str) -> Dict[str, Any]:
    """A fresh, empty store for one person."""
    return {"id": person, "entries": [], "last_modified": None}


def load_locations(person: str) -> Dict[str, Any]:
    """Load one person's whole location file, falling back to an empty store.

    A missing file is normal (nobody has logged for them yet). A corrupt or
    unexpectedly shaped file is logged and treated as empty rather than raising,
    so one bad file cannot take the endpoint down.
    """
    path = location_file(person)
    try:
        with open(path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except FileNotFoundError:
        return _empty_store(person)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s (%s); starting from an empty store", path, e)
        return _empty_store(person)

    if not isinstance(store, dict) or not isinstance(store.get("entries"), list):
        logger.error("Unexpected structure in %s; starting from an empty store", path)
        return _empty_store(person)
    return store


def save_locations(person: str, store: Dict[str, Any]) -> Path:
    """Persist one person's store, stamping the modification time.

    ``ensure_ascii=False`` keeps non-ASCII names and addresses readable in the
    file rather than escaped as ``\\u5a1c``.
    """
    store["id"] = person
    store["last_modified"] = _now()
    path = location_file(person)
    STATE_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    return path


def append_location(
    person: str,
    latitude: float,
    longitude: float,
    address: Optional[str] = None,
    time: Optional[str] = None,
    trigger: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one position to ``person``'s file and return the stored entry.

    ``time`` defaults to now and is stored verbatim otherwise; ``address`` and
    ``trigger`` — why this position was logged, e.g. "arrived home" — may both be
    None. The file is trimmed to the newest :data:`MAX_ENTRIES` entries.
    """
    store = load_locations(person)
    stamp = _now()
    entry = {
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "time": time or stamp,
        "recorded_at": stamp,
        "trigger": trigger,
    }
    entries = store.setdefault("entries", [])
    entries.append(entry)
    del entries[:-MAX_ENTRIES]  # keep the newest MAX_ENTRIES; a no-op under the cap
    save_locations(person, store)
    logger.debug("Location: %s at %s,%s (%s) trigger=%s",
                 person, latitude, longitude, address, trigger)
    return entry


def location_entries(person: str) -> List[Dict[str, Any]]:
    """Return a person's entries, oldest first (the order they were logged)."""
    return load_locations(person).get("entries") or []


def latest_location(person: str) -> Optional[Dict[str, Any]]:
    """Return the most recently logged entry for a person, or None."""
    entries = location_entries(person)
    return entries[-1] if entries else None


def parse_timestamp(value: Any) -> Optional[datetime.datetime]:
    """Best-effort parse of a stored timestamp into a naive local datetime.

    Accepts this server's own format first, then a few common spellings and
    ISO 8601 (so a caller's hand-written ``time`` can still be aged). Anything
    unrecognised returns None rather than raising — callers decide what to do
    with an entry they cannot date. Timezone-aware values are converted to local
    time and stripped, so every comparison stays naive like the store itself.
    """
    if isinstance(value, datetime.datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        stamp = None
        for fmt in (TIMESTAMP_FORMAT,) + _FALLBACK_TIMESTAMP_FORMATS:
            try:
                stamp = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if stamp is None:
            try:
                stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None

    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp


def entry_datetime(entry: Any) -> Optional[datetime.datetime]:
    """When an entry happened, for ageing purposes.

    Prefers ``recorded_at`` — this server wrote it, in a known format — and
    falls back to the caller's own ``time``. Returns None when neither can be
    parsed.
    """
    if not isinstance(entry, dict):
        return None
    for key in ("recorded_at", "time"):
        stamp = parse_timestamp(entry.get(key))
        if stamp is not None:
            return stamp
    return None


def trim_locations(person: str, keep: int) -> Tuple[int, int]:
    """Keep only a person's ``keep`` most recently logged entries.

    Returns ``(removed, kept)``. Timestamps are not consulted at all — this is
    purely positional, so an entry whose ``time`` cannot be parsed is treated
    like any other (unlike :func:`prune_locations`, which keeps those). ``keep``
    of 0 empties the history; the file itself stays, and is only rewritten when
    something was actually removed.
    """
    store = load_locations(person)
    entries = store.get("entries") or []
    removed = max(0, len(entries) - max(0, keep))

    if removed:
        store["entries"] = entries[removed:]
        save_locations(person, store)
        logger.debug("Trimmed %s location entries for %s (kept %s)",
                     removed, person, len(entries) - removed)
    return removed, len(entries) - removed


def prune_locations(person: str, cutoff: datetime.datetime) -> Tuple[int, int, int]:
    """Drop a person's entries older than ``cutoff``.

    Returns ``(removed, kept, undated)``. Entries whose timestamp cannot be
    parsed are **kept** and counted in ``undated``: deleting data this server
    cannot date would be worse than leaving it. The file is only rewritten when
    something was actually removed.
    """
    store = load_locations(person)
    kept: List[Dict[str, Any]] = []
    removed = 0
    undated = 0

    for entry in store.get("entries") or []:
        when = entry_datetime(entry)
        if when is None:
            undated += 1
            kept.append(entry)
        elif when < cutoff:
            removed += 1
        else:
            kept.append(entry)

    if removed:
        store["entries"] = kept
        save_locations(person, store)
        logger.debug("Pruned %s location entries for %s (cutoff %s)", removed, person, cutoff)
    return removed, len(kept), undated


if __name__ == "__main__":
    # Simple smoke test / demo — writes to state/<id>_loc.json.
    print("resolve_person({'id': 'Alex'}) ->", resolve_person({"id": "Alex"}))
    print("append ->", append_location("Alex", 37.334606, -122.009102, address="Apple Park"))
    print("latest ->", latest_location("Alex"))
    print("all    ->", location_entries("Alex"))
    print("parse  ->", parse_timestamp("2026-08-18 09:15:23.123"),
          "|", parse_timestamp("2026-08-18T09:15:23Z"), "|", parse_timestamp("whenever"))
    print("prune  ->", prune_locations("Alex", datetime.datetime.now()))
    print("unseen ->", latest_location("nobody"))
    print("Wrote to:", location_file("Alex"))
