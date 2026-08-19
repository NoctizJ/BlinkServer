#!/usr/bin/env python3
"""Phone notifications for logged locations, switchable per person.

When a position is logged (``POST /webhook/location/log``, see
:mod:`jobs.location_webhook`) this pushes a notification to the phone through
the same :func:`jobs.home_assistant_notify.notify_phone` wrapper the
arrival/departure webhooks use.

Two switches gate it, the same two-level shape the logging engine uses (see
:mod:`jobs.switches`):

  * the **master** switch — the ``notify_phone`` entry in
    ``configs/job_config.json``, shared with the ``/webhook/notify/*`` webhooks,
    so ``POST /jobs/notify_phone/disable`` silences every phone notification
    this server sends;
  * a **per-person** switch — one entry per id in
    ``configs/location_notify_config.json``::

        {
          "ids": { "娜": true, "Alex": false },
          "last_modified": "2026-08-18 21:04:11.221"
        }

Both must be on for a notification to go out. A person nobody has toggled yet is
auto-registered as enabled the first time they log a position, so they show up in
the file and can be turned off. Toggle them over HTTP with
``POST /location/notify/<id>/enable|disable|toggle``, and list them with
``GET /location/notify``.

The title and message live with every other notification's text, in
``configs/notify_config.json`` under ``location_log``::

    "location_log": {
      "title": "位置情報を記録",
      "message": "{id} is at {address} ({time})."
    }

Available placeholders: ``{id}``/``{name}``, ``{address}``, ``{latitude}``,
``{longitude}``, ``{time}`` and ``{maps_url}``. ``{address}`` falls back to the
coordinates when the logged position had no address. A request may override
``title``/``message`` per call, so precedence matches the other notifying job:
payload > notify_config.json > built-in default.

Nothing here raises: a notification that cannot be sent is reported in the
return value, so it never costs you the logged position.
"""

import logging
from typing import Any, Dict, Optional

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from jobs.switches import REPO_ROOT, all_switches, is_enabled, master_enabled, set_enabled
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from switches import REPO_ROOT, all_switches, is_enabled, master_enabled, set_enabled

logger = logging.getLogger(__name__)

# The per-person switches: configs/location_notify_config.json, "ids" section.
SWITCH_FILE = REPO_ROOT / "configs" / "location_notify_config.json"
IDS_SECTION = "ids"

# The job whose job_config.json entry is the master switch for every phone
# notification, shared with jobs/notify_phone.py.
MASTER_SWITCH = "notify_phone"

# The notify_config.json key holding this notification's title and message.
EVENT = "location_log"

# Used when notify_config.json has no "location_log" entry.
DEFAULT_TEXT = {"title": "位置情報を記録", "message": "{id} is at {address} ({time})."}


def enabled_for(person: str) -> bool:
    """Return whether ``person``'s notifications are on, registering new people."""
    return is_enabled(SWITCH_FILE, IDS_SECTION, person)


def set_enabled_for(person: str, enabled: bool) -> bool:
    """Turn ``person``'s notifications on or off; returns the value written."""
    logger.info("Location notifications for %s %s", person,
                "enabled" if enabled else "disabled")
    return set_enabled(SWITCH_FILE, IDS_SECTION, person, enabled)


def all_enabled() -> Dict[str, bool]:
    """Return every person's switch, keyed by id."""
    return all_switches(SWITCH_FILE, IDS_SECTION)


def _skipped(message: str) -> Dict[str, Any]:
    """The result for a notification that was deliberately not sent."""
    return {"status": "skipped", "message": message}


def notify_location(
    person: str,
    entry: Dict[str, Any],
    map_url: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Notify the phone that ``person``'s position was logged.

    ``entry`` is the stored entry (see :mod:`jobs.location_state`) and
    ``map_url`` the link to include as ``{maps_url}``. Returns the
    :func:`jobs.home_assistant_notify.notify_phone` result, or a ``"skipped"``
    result when a switch is off — never raises.
    """
    if not master_enabled(MASTER_SWITCH):
        return _skipped(f"phone notifications are off (master '{MASTER_SWITCH}' switch)")
    if not enabled_for(person):
        return _skipped(f"location notifications are off for {person}")

    latitude, longitude = entry.get("latitude"), entry.get("longitude")
    values = {
        "id": person,
        "name": person,
        "address": entry.get("address") or f"{latitude},{longitude}",
        "latitude": latitude,
        "longitude": longitude,
        "time": entry.get("time"),
        "maps_url": map_url or "",
    }

    # Precedence: payload > notify_config.json > built-in default.
    payload = payload if isinstance(payload, dict) else {}
    configured = load_event_text(EVENT)
    text = {
        field: fill_placeholders(
            payload.get(field) or configured.get(field) or DEFAULT_TEXT[field], values
        )
        for field in ("title", "message")
    }

    try:
        result = notify_phone(text["title"], text["message"])
    except Exception as e:  # missing/invalid home_assistant_config.json, network, ...
        error_msg = str(e)
        logger.error("location notification failed for %s: %s", person, error_msg)
        return {"status": "error", "error": "Notification failed", "message": error_msg}

    return {**result, "title": text["title"]}


if __name__ == "__main__":
    # Simple smoke test / demo — toggles the real switch file, sends nothing
    # unless a valid home_assistant_config.json is in place.
    demo_entry = {"latitude": 37.334606, "longitude": -122.009102,
                  "address": "Apple Park", "time": "2026-08-18 09:15:23.123"}
    print("all switches ->", all_enabled())
    print("enabled_for  ->", enabled_for("Alex"))
    print("disable      ->", set_enabled_for("Alex", False))
    print("skipped      ->", notify_location("Alex", demo_entry))
    print("re-enable    ->", set_enabled_for("Alex", True))
    print("notify       ->", notify_location("Alex", demo_entry,
                                            map_url="https://maps.apple.com/?ll=37.3,-122.0"))
