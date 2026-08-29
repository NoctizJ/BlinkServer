#!/usr/bin/env python3
"""Webhook job that reads and writes the presence store.

Two webhooks share this module (see config.json):

  - read(payload)  -> POST /webhook/presence/read
  - write(payload) -> POST /webhook/presence/write

``read`` reports who is home and who is away, from state/presence.json::

    {}                            # everyone in the default home ("A")
    {"id": "娜"}                  # just one person, in the default home
    {"home": "M"}                 # everyone in home M
    {"home": "M", "id": "Sam"}    # one person in home M
    {"home": "all"}               # every home at once

Every read result carries a ready-to-display ``message`` — a formatted plain
text summary (see :func:`format_presence` and :func:`format_all_homes`) for
putting on a dashboard, in a notification, or in a terminal.

The name lists in a read result are ``home_id`` (who is home) and ``away_id``
(who is away); ``home`` names the house the result describes, matching the key
a caller posts.

``write`` sets a person's state by hand — useful to seed the store or to fix it
up when a leaving/arriving webhook was missed::

    {"id": "娜", "state": "home"}              # "id" defaults to DEFAULT_PERSON
    {"state": "away", "event": "..."}          # "event" defaults to "manual_write"
    {"state": "home", "home": "M"}             # "home" defaults to DEFAULT_HOME

``state`` accepts a few friendly spellings: home/in/true for home, and
away/left/out/not_home/false for away.

A post with no ``home`` is attributed to ``DEFAULT_HOME`` ("A"), so a single-home
setup never has to mention one. Homes are independent: the same ``id`` in two
homes is two separate entries.

This job only ever touches the presence file — it deliberately does NOT arm or
disarm the alarm panel. Recording a state here is bookkeeping, not an
automation trigger; the panel is only changed by /webhook/blink/* and (when
enabled in notify_config.json) the /webhook/notify/* handlers.

Both handlers report problems in their return value rather than raising, so a
bad payload gets a JSON error instead of a 500.
"""

import logging
from typing import Any, Dict, Optional

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.log_engine import log as write_log
    from jobs.presence_state import (
        ALL_HOMES,
        STATE_AWAY,
        STATE_HOME,
        all_homes,
        all_states,
        get_state,
        resolve_home,
        resolve_person,
        set_state,
    )
    from jobs.text_format import display_width, pad, rule
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import log as write_log
    from presence_state import (
        ALL_HOMES,
        STATE_AWAY,
        STATE_HOME,
        all_homes,
        all_states,
        get_state,
        resolve_home,
        resolve_person,
        set_state,
    )
    from text_format import display_width, pad, rule

logger = logging.getLogger(__name__)

# Accepted spellings for each state, so callers need not know the exact values.
STATE_ALIASES = {
    STATE_HOME: STATE_HOME,
    "in": STATE_HOME,
    "true": STATE_HOME,
    STATE_AWAY: STATE_AWAY,
    "left": STATE_AWAY,
    "out": STATE_AWAY,
    "not_home": STATE_AWAY,
    "false": STATE_AWAY,
}

# Event recorded when a state is set through this webhook rather than by a
# leaving/arriving notification.
MANUAL_EVENT = "manual_write"


def _normalize_state(value: Any) -> Optional[str]:
    """Map a caller's state spelling to STATE_HOME/STATE_AWAY, or None."""
    if isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    return STATE_ALIASES.get(value.strip().lower())


def _describe(entry: Dict[str, Any]) -> str:
    """Render one entry's state, timestamp and originating event."""
    state = entry.get("state") or "unknown"
    since = entry.get("last_updated") or "unknown"
    event = entry.get("event")
    return f"{state} since {since}" + (f" ({event})" if event else "")


def format_presence(people: Dict[str, Any], person: Optional[str] = None) -> str:
    """Render the presence store as a plain text summary for display.

    With ``person`` set, renders that one person as a single line::

        娜 is home since 2026-08-03 18:42:11.482 (arriving_home)

    Otherwise renders the whole store: a count, the home/away name lists, and
    one aligned row per person::

        Presence — 3 people
        ----------------------------------------------------
        Home (2): 娜, Sam
        Away (1): Alex

        Alex  away  since 2026-08-03 08:07:53.119  (leaving_home)
        Sam   home  since 2026-08-03 17:20:04.008  (manual_write)
        娜    home  since 2026-08-03 18:42:11.482  (arriving_home)
    """
    if person is not None:
        entry = people.get(person)
        if not entry:
            return f"{person}: no presence recorded yet."
        return f"{person} is {_describe(entry)}"

    if not people:
        return "Presence — nobody recorded yet."

    home = sorted(n for n, e in people.items() if e.get("state") == STATE_HOME)
    away = sorted(n for n, e in people.items() if e.get("state") == STATE_AWAY)

    name_width = max(display_width(n) for n in people)
    state_width = max(display_width(e.get("state") or "unknown") for e in people.values())
    rows = [
        f"{pad(name, name_width)}  {pad(entry.get('state') or 'unknown', state_width)}  "
        f"since {entry.get('last_updated') or 'unknown'}"
        + (f"  ({entry['event']})" if entry.get("event") else "")
        for name, entry in sorted(people.items())
    ]

    count = len(people)
    header = f"Presence — {count} {'person' if count == 1 else 'people'}"
    summary = [f"Home ({len(home)}): {', '.join(home) or '-'}",
               f"Away ({len(away)}): {', '.join(away) or '-'}"]

    return "\n".join([header, rule([header] + summary + rows)] + summary + [""] + rows)


def format_all_homes(homes: Dict[str, Dict[str, Any]]) -> str:
    """Render every home as one plain text summary, tagged by home.

    Laid out like :func:`format_presence`, with a ``[home]`` tag on each line so
    the houses stay distinguishable::

        Presence — 3 people across 2 homes
        ---------------------------------------------------
        [A] Home (0): -    Away (2): Alex, 娜
        [M] Home (1): Sam  Away (0): -

        [A] Alex  away  since 2026-08-03 08:07:53.119  (leaving_home)
        [A] 娜    away  since 2026-08-18 22:04:21.680  (leaving_home)
        [M] Sam   home  since 2026-08-19 09:12:00.001  (arriving_home)

    Homes with nobody in them are left out, so a home that was created and then
    emptied does not clutter the summary.
    """
    total = sum(len(people) for people in homes.values())
    if not total:
        return "Presence — nobody recorded yet."

    occupied = {home: people for home, people in sorted(homes.items()) if people}

    tag_width = max(display_width(f"[{home}]") for home in occupied)
    name_width = max(display_width(n) for people in occupied.values() for n in people)
    state_width = max(
        display_width(entry.get("state") or "unknown")
        for people in occupied.values()
        for entry in people.values()
    )

    home_parts, away_parts = [], []
    for people in occupied.values():
        at_home = sorted(n for n, e in people.items() if e.get("state") == STATE_HOME)
        away = sorted(n for n, e in people.items() if e.get("state") == STATE_AWAY)
        home_parts.append(f"Home ({len(at_home)}): {', '.join(at_home) or '-'}")
        away_parts.append(f"Away ({len(away)}): {', '.join(away) or '-'}")
    home_width = max(display_width(part) for part in home_parts)

    summary = [
        f"{pad(f'[{home}]', tag_width)} {pad(home_parts[i], home_width)}  {away_parts[i]}"
        for i, home in enumerate(occupied)
    ]

    rows = [
        f"{pad(f'[{home}]', tag_width)} {pad(name, name_width)}  "
        f"{pad(entry.get('state') or 'unknown', state_width)}  "
        f"since {entry.get('last_updated') or 'unknown'}"
        + (f"  ({entry['event']})" if entry.get("event") else "")
        for home, people in occupied.items()
        for name, entry in sorted(people.items())
    ]

    homes_count = len(occupied)
    header = (
        f"Presence — {total} {'person' if total == 1 else 'people'} "
        f"across {homes_count} {'home' if homes_count == 1 else 'homes'}"
    )
    return "\n".join([header, rule([header] + summary + rows)] + summary + [""] + rows)


def _name_lists(people: Dict[str, Any]) -> Dict[str, Any]:
    """Split a home's people into the ``home_id``/``away_id`` name lists."""
    return {
        "home_id": sorted(n for n, e in people.items() if e.get("state") == STATE_HOME),
        "away_id": sorted(n for n, e in people.items() if e.get("state") == STATE_AWAY),
    }


def read(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/presence/read (and GET /presence).

    Reads one home — ``payload["home"]``, defaulting to ``DEFAULT_HOME`` — or
    every home at once when ``home`` is ``"all"`` (matched case-insensitively,
    which makes "all" a reserved home name).

    Within a home, no ``id`` returns everyone's entry plus the ``home_id`` and
    ``away_id`` name lists; an ``id`` returns just that person (``presence`` is
    null if they have never been seen). Either way ``message`` holds a
    formatted, ready-to-display summary. An unknown home reads as empty rather
    than as an error, so a dashboard polling it still gets a normal response.
    """
    logger.debug("presence read payload: %s", payload)
    payload = payload if isinstance(payload, dict) else {}
    home = resolve_home(payload)

    if home.lower() == ALL_HOMES:
        homes = all_homes()
        return {
            "status": "ok",
            "home": ALL_HOMES,
            "count": sum(len(people) for people in homes.values()),
            "homes": {
                name: {"count": len(people), **_name_lists(people), "people": people}
                for name, people in homes.items()
            },
            "message": format_all_homes(homes),
        }

    people = all_states(home)

    if payload.get("id"):
        person = resolve_person(payload)
        entry = get_state(person, home=home)
        return {
            "status": "ok",
            "home": home,
            "id": person,
            "presence": entry,
            "state": entry["state"] if entry else None,
            "message": format_presence(people, person=person),
        }

    return {
        "status": "ok",
        "home": home,
        "count": len(people),
        **_name_lists(people),
        "people": people,
        "message": format_presence(people),
    }


def write(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/presence/write.

    Requires a ``state`` field; ``id`` defaults to the store's default person,
    ``home`` to the store's default home, and ``event`` to ``MANUAL_EVENT``.
    """
    logger.debug("presence write payload: %s", payload)

    if not isinstance(payload, dict):
        return {
            "status": "error",
            "error": "Invalid payload format",
            "message": "Payload must be a JSON object",
        }

    if "state" not in payload:
        return {
            "status": "error",
            "error": "Missing state",
            "message": f"Payload must include a 'state' field ('{STATE_HOME}' or '{STATE_AWAY}')",
        }

    presence = _normalize_state(payload.get("state"))
    if presence is None:
        return {
            "status": "error",
            "error": "Invalid state",
            "message": (
                f"Invalid state {payload.get('state')!r} - "
                f"must be '{STATE_HOME}' or '{STATE_AWAY}'"
            ),
        }

    person = resolve_person(payload)
    home = resolve_home(payload)
    event = payload.get("event") or MANUAL_EVENT

    # "all" is how a read asks for every home, so a home cannot be called that —
    # anything written there would be unreadable on its own.
    if home.lower() == ALL_HOMES:
        return {
            "status": "error",
            "error": "Reserved home name",
            "message": f"'{ALL_HOMES}' is reserved for reading every home and cannot be written to",
        }

    try:
        entry = set_state(person, presence, event=str(event), home=home)
    except Exception as e:
        error_msg = str(e)
        logger.error("presence write failed for %s in %s: %s", person, home, error_msg)
        write_log("blink", f"PRESENCE write ERROR ({person}@{home}): {error_msg}")
        return {
            "status": "error",
            "error": "Presence update failed",
            "message": error_msg,
        }

    write_log(
        "blink",
        f"PRESENCE write: person={person} home={home} presence={presence} event={event}",
    )
    return {"status": "ok", "id": person, "home": home, "presence": entry}


if __name__ == "__main__":
    # Simple smoke test / demo — writes to state/presence.json.
    print("write         ->", write({"id": "Alex", "state": "left"}))
    print("write default ->", write({"state": "home"}))
    print("write home M  ->", write({"id": "Sam", "state": "home", "home": "M"}))
    print("read one      ->", read({"id": "Alex"}))
    print("bad state     ->", write({"state": "somewhere"}))
    print("missing state ->", write({}))
    print("reserved home ->", write({"state": "home", "home": "all"}))
    print("\nformatted message (home A):\n")
    print(read({})["message"])
    print("\nformatted message (every home):\n")
    print(read({"home": "all"})["message"])
