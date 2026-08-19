#!/usr/bin/env python3
"""Webhook job that logs and reads back where somebody is.

The location counterpart of the ``log`` job: where that one appends text to a
shared log file, this one records positions **per person, in that person's own
file** (``state/<id>_loc.json``, see :mod:`jobs.location_state`). Nothing is
written to ``logs/default.log`` or any other existing log, and the logging
engine's master/per-type switches do not gate it.

Four webhooks share this module (see config.json):

  - log(payload)     -> POST /webhook/location/log
  - fetch(payload)   -> POST /webhook/location/fetch   (and GET /location)
  - history(payload) -> POST /webhook/location/history (and GET /location/history)
  - purge(payload)   -> POST /webhook/location/purge

``log`` records one position::

    {
        "id": "娜",                            # optional - defaults to 娜
        "latitude": 37.334606,                  # required
        "longitude": -122.009102,               # required
        "address": "Apple Park, Cupertino",     # optional
        "time": "2026-08-18 09:15:23.123"       # optional - defaults to now
    }

``latitude``/``longitude`` also accept the aliases ``lat`` and ``lon``/``lng``/
``long``, and may arrive as numeric strings (Shortcuts sends text). ``time`` is
stored verbatim, in whatever format the caller uses.

``fetch`` returns the latest position as JSON, with a ready-to-open map link for
Apple Maps (``maps_url``) and one for Google Maps (``google_maps_url``). Pass
``n`` for an ``entries`` list of recent positions too.

``history`` returns the whole history as a formatted text table in ``message``,
like ``/logs/{type}/read`` and ``GET /presence``.

``purge`` deletes history either by count — ``{"records": 25}`` keeps the 25 most
recently logged entries — or by age — ``{"days": 30}`` keeps the last 30 days,
aged by ``recorded_at``. ``records`` wins if both are given; with neither, the
:data:`DEFAULT_RECORDS` most recent entries are kept.

Every handler resolves ``id`` the way the presence webhooks do, so a post with no
``id`` is attributed to ``娜``. Like presence, they report problems in their
return value rather than raising, so a bad payload gets a JSON error instead of
a 500.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.location_notify import notify_location
    from jobs.location_state import (
        TIMESTAMP_FORMAT,
        append_location,
        location_entries,
        location_file,
        prune_locations,
        trim_locations,
    )
    from jobs.presence_state import resolve_person
    from jobs.text_format import display_width, pad, rule
except ImportError:  # pragma: no cover - allows running this file directly
    from location_notify import notify_location
    from location_state import (
        TIMESTAMP_FORMAT,
        append_location,
        location_entries,
        location_file,
        prune_locations,
        trim_locations,
    )
    from presence_state import resolve_person
    from text_format import display_width, pad, rule

logger = logging.getLogger(__name__)

# Latitude/longitude bounds, in degrees.
LAT_LIMIT = 90.0
LON_LIMIT = 180.0

# Friendly spellings accepted for the coordinate fields, so a Shortcut or
# automation need not know the canonical names.
LATITUDE_KEYS = ("latitude", "lat")
LONGITUDE_KEYS = ("longitude", "lon", "lng", "long")

# How many recent entries `purge` keeps when the caller asks for neither
# `records` nor `days`.
DEFAULT_RECORDS = 10

# Friendly spellings for `purge`'s "how many to keep" input.
RECORDS_KEYS = ("records", "keep")

# Shown in the history table for an entry with no address, and in place of a
# coordinate that a hand-edited file left out.
NO_ADDRESS = "-"
MISSING_COORDINATE = "?"

# Column widths for the history table's coordinate pair: "-90.000000" and
# "-180.000000" are the longest values either can take at six decimal places.
_LAT_WIDTH = 10
_LON_WIDTH = 11

# Gap between the history table's columns.
_GAP = "   "


# --------------------------------------------------------------------------
# Reading a caller's payload
# --------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    """True for a real number — ``True``/``False`` are ints but are not numbers."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any) -> Optional[str]:
    """Return a stripped string, or None for missing/blank values."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coordinate(value: Any, name: str, limit: float) -> Tuple[Optional[float], Optional[str]]:
    """Coerce a coordinate to a float within ``±limit``.

    Returns ``(number, None)`` on success and ``(None, message)`` on failure, so
    callers can report a bad value instead of raising. Numeric strings are
    accepted — Shortcuts and many automations send every field as text. ``nan``
    and ``inf`` fail the range check (no comparison with them is true), so they
    are rejected along with out-of-range degrees.
    """
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None, f"Payload must include a numeric '{name}'"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"Invalid {name} {value!r} - must be a number"
    if not -limit <= number <= limit:
        return None, f"Invalid {name} {value!r} - must be between -{limit:g} and {limit:g}"
    return number, None


def _first(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """Return the first of ``keys`` present in the payload, else None."""
    return next((payload[key] for key in keys if payload.get(key) is not None), None)


def _coordinates(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Pull latitude/longitude out of a payload, accepting the key aliases.

    Returns ``(latitude, longitude, error)``; ``error`` is None when both are
    valid.
    """
    latitude, error = _coordinate(_first(payload, LATITUDE_KEYS), "latitude", LAT_LIMIT)
    if error:
        return None, None, error
    longitude, error = _coordinate(_first(payload, LONGITUDE_KEYS), "longitude", LON_LIMIT)
    if error:
        return None, None, error
    return latitude, longitude, None


def _count(value: Any) -> Optional[int]:
    """Coerce a caller's ``n`` to a positive int, or None when unusable.

    Query strings arrive as text, so ``"5"`` has to work as well as ``5``. None
    means "no count given" — the reader decides what that implies (the latest
    entry only, or the whole history).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _records(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """Coerce ``purge``'s ``records`` input — how many recent entries to keep.

    Returns ``(count, None)``, ``(None, None)`` when the caller did not ask for a
    count, or ``(None, message)`` for something unusable. Numeric strings are
    accepted; ``0`` is allowed and means "keep none". Negatives are refused.
    """
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None, None
    try:
        records = int(str(value).strip())
    except (TypeError, ValueError):
        return None, f"Invalid records {value!r} - must be a whole number of entries"
    if records < 0:
        return None, f"Invalid records {value!r} - must be zero or more"
    return records, None


def _days(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Coerce ``purge``'s ``days`` input — how far back to keep.

    Returns ``(days, None)``, ``(None, None)`` when the caller did not ask for an
    age, or ``(None, message)`` for something unusable. Numeric strings are
    accepted. Negatives are refused — a negative window would purge entries newer
    than now, which is never what anyone means.
    """
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None, None
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None, f"Invalid days {value!r} - must be a number of days"
    if not days >= 0:  # also catches nan, whose comparisons are all False
        return None, f"Invalid days {value!r} - must be zero or more"
    if days == float("inf"):
        return None, f"Invalid days {value!r} - must be a finite number of days"
    return days, None


def _bad_payload() -> Dict[str, Any]:
    """The error every handler returns for a payload that is not a JSON object."""
    return {
        "status": "error",
        "error": "Invalid payload format",
        "message": "Payload must be a JSON object",
    }


def _failed(error: str, person: str, exception: Exception) -> Dict[str, Any]:
    """Report a store operation that raised, without letting it become a 500."""
    logger.error("%s for %s: %s", error, person, exception)
    return {"status": "error", "error": error, "message": str(exception)}


# --------------------------------------------------------------------------
# Map links
# --------------------------------------------------------------------------
def maps_url(latitude: float, longitude: float, label: Optional[str] = None) -> str:
    """Build an Apple Maps URL that drops a labelled pin on a coordinate.

    ``https://maps.apple.com/?ll=<lat>,<lon>&q=<label>`` — hand this to the
    Shortcuts "Open URLs" action and Maps opens on the pin. The label is only the
    pin's name (with ``ll`` present, Maps does not treat ``q`` as a search). The
    comma in ``ll`` is left literal for readability; the label is arbitrary text
    and is fully percent-encoded, so addresses with spaces, commas or non-ASCII
    characters are safe.
    """
    # Encoded separately: `ll` keeps its comma, the label keeps nothing.
    query = urlencode({"ll": f"{latitude},{longitude}"}, safe=",")
    if label:
        query += "&" + urlencode({"q": str(label)})
    return "https://maps.apple.com/?" + query


def google_maps_url(latitude: float, longitude: float) -> str:
    """Build a Google Maps URL that drops a pin on a coordinate.

    ``https://www.google.com/maps?q=<lat>,<lon>`` — the cross-platform companion
    to :func:`maps_url`, for anything that is not an Apple device (an Android
    phone, a browser, a chat message). Google takes the coordinates as the search
    query, so there is no separate pin label to pass.
    """
    return "https://www.google.com/maps?" + urlencode(
        {"q": f"{latitude},{longitude}"}, safe=","
    )


# --------------------------------------------------------------------------
# The history table
# --------------------------------------------------------------------------

def _column(value: Any, width: int) -> str:
    """Right-align one coordinate to six decimal places, or "?" if unusable."""
    if _is_number(value):
        return f"{value:>{width}.6f}"
    return f"{MISSING_COORDINATE:>{width}}"


def _pair(entry: Dict[str, Any]) -> str:
    """Render an entry's coordinate pair as an aligned ``lat, lon`` column."""
    return (f"{_column(entry.get('latitude'), _LAT_WIDTH)}, "
            f"{_column(entry.get('longitude'), _LON_WIDTH)}")


def format_history(
    person: str,
    entries: List[Dict[str, Any]],
    total: Optional[int] = None,
) -> str:
    """Render a person's entries as a plain text table, newest first::

        Location history — Alex — 3 entries (newest first)
        ------------------------------------------------------------------------
        2026-08-18 09:15:23.123    37.334606, -122.009102   Apple Park, Cupertino
        2026-08-17 20:04:55.545    51.501400,   -0.141900   Buckingham Palace
        2026-08-16 08:07:53.119    37.331800, -122.031200   -

    ``entries`` is already in display order. ``total`` is how many the store
    holds, so a capped read can say "20 of 340 entries"; pass None when the
    entries are everything. The time shown is each entry's own ``time`` — the
    caller's timestamp.
    """
    if not entries:
        return f"Location history — {person} — nothing logged yet."

    count = len(entries)
    truncated = total is not None and total != count
    shown = f"{count} of {total}" if truncated else str(count)
    # "1 entry", but "1 of 2 entries" — a capped read pluralizes on the total.
    noun = "entry" if (total if truncated else count) == 1 else "entries"
    header = f"Location history — {person} — {shown} {noun} (newest first)"

    times = [str(entry.get("time") or "unknown") for entry in entries]
    time_width = max(display_width(t) for t in times)
    rows = [
        pad(time, time_width) + _GAP + _pair(entry) + _GAP
        + str(entry.get("address") or NO_ADDRESS)
        for time, entry in zip(times, entries)
    ]

    return "\n".join([header, rule([header] + rows)] + rows)


# --------------------------------------------------------------------------
# The four webhook handlers
# --------------------------------------------------------------------------

def log(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/location/log.

    Appends one position to the caller's ``state/<id>_loc.json`` and returns the
    stored entry, together with ready-to-open map links. Also pushes a phone
    notification, unless it is switched off for this person or the master
    ``notify_phone`` switch is off — see :mod:`jobs.location_notify`. A
    notification that cannot be sent is reported in ``notify`` and never fails
    the write.
    """
    logger.debug("location log payload: %s", payload)

    if not isinstance(payload, dict):
        return _bad_payload()

    latitude, longitude, error = _coordinates(payload)
    if error:
        return {"status": "error", "error": "Invalid coordinates", "message": error}

    person = resolve_person(payload)
    address = _text(payload.get("address"))

    try:
        entry = append_location(person, latitude, longitude, address=address,
                                time=_text(payload.get("time")))
    except Exception as e:  # unwritable state dir, unusable id, ...
        return _failed("Location write failed", person, e)

    apple_link = maps_url(latitude, longitude, address or person)
    return {
        "status": "ok",
        "id": person,
        "location": entry,
        "file": location_file(person).name,
        "maps_url": apple_link,
        "google_maps_url": google_maps_url(latitude, longitude),
        "notify": notify_location(person, entry, map_url=apple_link, payload=payload),
        "message": f"Logged {person} at {latitude},{longitude}"
                   + (f" ({address})" if address else "")
                   + f" at {entry['time']}",
    }


def fetch(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for GET /location and POST /webhook/location/fetch.

    Returns the person's latest latitude, longitude, address and time, plus a
    map link for Apple Maps and one for Google Maps. An ``id`` that has never
    been logged is **not** an error: the response comes back with
    ``"found": false`` and null fields, so a Shortcut sees a normal 200 instead
    of failing on a 404.

    "Latest" means most recently *logged* — entries are appended in call order
    and never re-sorted by their ``time`` field.
    """
    logger.debug("location fetch payload: %s", payload)

    person = resolve_person(payload)  # a non-dict payload falls back to 娜
    entries = location_entries(person)
    entry = entries[-1] if entries else None

    result = {
        "status": "ok",
        "id": person,
        "found": entry is not None,
        "latitude": None,
        "longitude": None,
        "address": None,
        "time": None,
        "recorded_at": None,
        "maps_url": None,
        "google_maps_url": None,
        "message": f"{person}: no location logged yet.",
        "file": location_file(person).name,
    }

    if entry is not None:
        latitude, longitude = entry.get("latitude"), entry.get("longitude")
        address = entry.get("address")
        # A hand-edited file may be missing coordinates; then there is no pin to
        # open, but the rest of the entry is still worth returning.
        plottable = _is_number(latitude) and _is_number(longitude)
        result.update({
            "latitude": latitude,
            "longitude": longitude,
            "address": address,
            "time": entry.get("time"),
            "recorded_at": entry.get("recorded_at"),
            "maps_url": maps_url(latitude, longitude, address or person) if plottable else None,
            "google_maps_url": google_maps_url(latitude, longitude) if plottable else None,
            "message": f"{person} was at {address or f'{latitude},{longitude}'} "
                       f"at {entry.get('time')}",
        })

    count = _count(payload.get("n") if isinstance(payload, dict) else None)
    if count:
        result["entries"] = list(reversed(entries[-count:]))

    return result


def history(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for GET /location/history and POST /webhook/location/history.

    ``message`` holds the formatted table (see :func:`format_history`); the rest
    is bookkeeping for callers that want it. Everything is returned unless ``n``
    caps it at the most recent entries. Never raises for an unknown person —
    they simply have no history yet.
    """
    logger.debug("location history payload: %s", payload)

    person = resolve_person(payload)  # a non-dict payload falls back to 娜
    entries = location_entries(person)
    total = len(entries)

    count = _count(payload.get("n") if isinstance(payload, dict) else None)
    if count:
        entries = entries[-count:]

    newest_first = list(reversed(entries))
    return {
        "status": "ok",
        "id": person,
        "count": len(newest_first),
        "total": total,
        "file": location_file(person).name,
        "message": format_history(person, newest_first, total=total),
    }


def purge(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/location/purge.

    Deletes history two ways, and reports what it removed and kept:

      - ``{"records": 25}`` keeps the 25 most recently logged entries;
      - ``{"days": 30}`` keeps everything logged in the last 30 days.

    **``records`` wins when both are given** — ``days`` is echoed back but not
    applied, and ``mode`` says which was used. With neither, the default is to
    keep the :data:`DEFAULT_RECORDS` most recent entries.

    Counting by records is positional and ignores timestamps entirely. Ageing by
    days uses ``recorded_at`` — the timestamp this server wrote — and the
    caller's own ``time`` only as a fallback, because ``time`` is stored in
    whatever format the caller sent; an entry whose timestamp cannot be parsed at
    all is kept and counted in ``undated``, since deleting data this server
    cannot date would be worse than leaving it.

    ``records: 0`` and ``days: 0`` both empty the history; the file itself stays.
    """
    logger.debug("location purge payload: %s", payload)

    if payload is not None and not isinstance(payload, dict):
        return _bad_payload()
    payload = payload if isinstance(payload, dict) else {}

    records, error = _records(_first(payload, RECORDS_KEYS))
    if error:
        return {"status": "error", "error": "Invalid records", "message": error}
    days, error = _days(payload.get("days"))
    if error:
        return {"status": "error", "error": "Invalid days", "message": error}

    # Neither asked for: keep the newest DEFAULT_RECORDS entries.
    if records is None and days is None:
        records = DEFAULT_RECORDS

    person = resolve_person(payload)
    result = {
        "status": "ok",
        "id": person,
        "mode": "records" if records is not None else "days",
        "records": records,
        "days": days,
        "cutoff": None,
        "undated": 0,
        "file": location_file(person).name,
    }

    try:
        if records is not None:
            removed, kept = trim_locations(person, records)
            ignored = " (days ignored)" if days is not None else ""
            message = (f"Purged {removed} {_entries(removed)} beyond the {records} most "
                       f"recent for {person}; {kept} kept.{ignored}")
        else:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
            removed, kept, undated = prune_locations(person, cutoff)
            result.update({"cutoff": cutoff.strftime(TIMESTAMP_FORMAT)[:-3],
                           "undated": undated})
            message = (f"Purged {removed} {_entries(removed)} older than {days:g} "
                       f"{'day' if days == 1 else 'days'} for {person}; {kept} kept.")
            if undated:
                message += f" {undated} kept without a usable timestamp."
    except Exception as e:  # unwritable state dir, unusable id, ...
        return _failed("Location purge failed", person, e)

    return {**result, "removed": removed, "kept": kept, "message": message}


def _entries(count: int) -> str:
    """"entry" or "entries", for a count."""
    return "entry" if count == 1 else "entries"


if __name__ == "__main__":
    # Simple smoke test / demo — writes to state/<id>_loc.json.
    print("log      ->", log({"id": "Alex", "latitude": 37.334606,
                              "longitude": -122.009102, "address": "Apple Park",
                              "time": "2026-08-18 09:15:23.123"}))
    print("defaults ->", log({"latitude": "37.3318", "lon": "-122.0312"}))
    print("missing  ->", log({"latitude": 37.334606}))
    print("bad lat  ->", log({"latitude": 99, "longitude": 0}))
    print("not json ->", log("nope"))
    print("fetch    ->", fetch({"id": "Alex"}))
    print("unknown  ->", fetch({"id": "nobody"}))
    print("purge    ->", purge({"id": "Alex"}))                    # keep 10 newest
    print("records  ->", purge({"id": "Alex", "records": "1"}))
    print("days     ->", purge({"id": "Alex", "days": 30}))
    print("both     ->", purge({"id": "Alex", "records": 5, "days": 30}))
    print("bad days ->", purge({"id": "Alex", "days": "ages"}))
    print("bad recs ->", purge({"id": "Alex", "records": -1}))
    print()
    print(history({"id": "Alex"})["message"])
