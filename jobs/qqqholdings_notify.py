#!/usr/bin/env python3
"""Phone notification for a change in the QQQ holdings.

:func:`notify_changes` is called by the QQQ holdings job after a run that found new
data in which a ticker was added to, or removed from, the fund. It builds the text
and sends it through :func:`jobs.home_assistant_notify.notify_phone`, so it goes to the
same devices and honours the Home Assistant ``notify`` switch. A device can opt out
with ``"disable": ["holdings_change"]`` in ``configs/home_assistant_entities.json``,
and the master ``notify_phone`` job switch in ``job_switches.json`` turns it off too.

The title and message come from the ``qqqholdings_change`` entry of
``configs/notify_config.json`` when there is one, with these placeholders:
``{as_of}``, ``{previous_as_of}``, ``{added_count}``, ``{removed_count}`` and
``{changes}`` (the "Added: ..." / "Removed: ..." lines). Otherwise the defaults below.

Nothing here raises: a notification is best effort and must never undo a saved report.
"""

import logging
from typing import Any, Dict

from jobs.home_assistant_notify import fill_placeholders, load_event_text, notify_phone
from jobs.switches import master_enabled

logger = logging.getLogger(__name__)

# The notify_config.json key for this notification's text, and the NOTIFY_GROUPS entry
# (home_assistant_notify.py) that lets a device opt out of it.
EVENT = "qqqholdings_change"

# The job whose job_switches.json entry is the master switch for every phone
# notification, shared with jobs/notify_phone.py and jobs/location_notify.py.
MASTER_SWITCH = "notify_phone"

DEFAULT_TEXT = {"title": "QQQ holdings changed — {as_of}", "message": "{changes}"}


def _changes_text(report: dict) -> str:
    """The 'Added: ...' and 'Removed: ...' lines, leaving out a side with nothing."""
    lines = []
    if report["added"]:
        lines.append("Added: " + ", ".join(
            f"{r['ticker']} ({r['name']}, {r['weight']:.2f}%)" for r in report["added"]))
    if report["removed"]:
        lines.append("Removed: " + ", ".join(
            f"{r['ticker']} ({r['name']}, was {r['weight']:.2f}%)" for r in report["removed"]))
    return "\n".join(lines)


def notify_changes(report: dict) -> Dict[str, Any]:
    """Notify the phone that tickers were added or removed; never raises.

    ``report`` is a report dict from the QQQ holdings job. Returns the
    :func:`notify_phone` result plus the ``title``; a ``"skipped"`` result when there
    is nothing to say or a switch is off; or an ``"error"`` result when sending fails.
    """
    if not (report["added"] or report["removed"]):
        return {"status": "skipped", "message": "no tickers were added or removed"}
    if not master_enabled(MASTER_SWITCH):
        return {"status": "skipped",
                "message": f"phone notifications are off (master '{MASTER_SWITCH}' switch)"}

    try:
        values = {
            "as_of": report["as_of"],
            "previous_as_of": report["previous_as_of"],
            "added_count": len(report["added"]),
            "removed_count": len(report["removed"]),
            "changes": _changes_text(report),
        }
        configured = load_event_text(EVENT)
        title = fill_placeholders(configured.get("title") or DEFAULT_TEXT["title"], values)
        message = fill_placeholders(configured.get("message") or DEFAULT_TEXT["message"], values)
        result = notify_phone(title, message, event=EVENT)
    except Exception as exc:  # missing/invalid home_assistant_config.json, network, ...
        logger.error("QQQ holdings notification failed: %s", exc)
        return {"status": "error", "error": "Notification failed", "message": str(exc)}
    return {**result, "title": title}
