#!/usr/bin/env python3
"""Presence state store for Blink Server.

Persists, per person, whether they are currently home or away. The state lives
in a single JSON file (``state/presence.json``) so it survives restarts:

    {
      "people": {
        "娜": {
          "state": "home",
          "event": "arriving_home",
          "last_updated": "2026-08-03 18:42:11.482"
        }
      },
      "last_modified": "2026-08-03 18:42:11.482"
    }

The identity comes from the ``id`` field of a webhook payload; when a post has
no ``id``, it is attributed to ``DEFAULT_PERSON`` ("娜").

Usage:
    from jobs.presence_state import resolve_person, set_state, get_state

    person = resolve_person(payload)      # payload["id"] or "娜"
    set_state(person, STATE_AWAY, event="leaving_home")
    get_state(person)                     # -> {"state": "away", ...}
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The repo root is the parent of the jobs/ folder that holds this module.
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
STATE_FILE = STATE_DIR / "presence.json"

# Payload key carrying the identity of whoever left/arrived.
ID_KEY = "id"

# Attribution for posts that omit the id.
DEFAULT_PERSON = "娜"

# The two presence states.
STATE_HOME = "home"
STATE_AWAY = "away"


def resolve_person(payload: Optional[Dict[str, Any]]) -> str:
    """Return the person named by ``payload["id"]``, or ``DEFAULT_PERSON``.

    A missing, non-string, or blank ``id`` falls back to ``DEFAULT_PERSON``, so
    an unlabelled webhook post is still attributed to somebody.
    """
    if isinstance(payload, dict):
        person = payload.get(ID_KEY)
        if isinstance(person, (str, int, float)) and str(person).strip():
            return str(person).strip()
    return DEFAULT_PERSON


def load_state() -> Dict[str, Any]:
    """Load the whole presence file, falling back to an empty store."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"people": {}, "last_modified": None}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s (%s); starting from an empty store", STATE_FILE, e)
        return {"people": {}, "last_modified": None}

    if not isinstance(state, dict) or not isinstance(state.get("people"), dict):
        logger.error("Unexpected structure in %s; starting from an empty store", STATE_FILE)
        return {"people": {}, "last_modified": None}
    return state


def save_state(state: Dict[str, Any]) -> None:
    """Persist the presence file, stamping the modification time.

    ``ensure_ascii=False`` keeps non-ASCII names (e.g. "娜") readable in the
    file rather than escaped as ``\\u5a1c``.
    """
    state["last_modified"] = _now()
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def set_state(person: str, presence: str, event: Optional[str] = None) -> Dict[str, Any]:
    """Record that ``person`` is now home or away and persist it.

    Args:
        person: The person's name (see :func:`resolve_person`).
        presence: ``STATE_HOME`` or ``STATE_AWAY``.
        event: Optional event name that caused the change, e.g. "leaving_home".

    Returns:
        dict: The stored entry for that person.
    """
    if presence not in (STATE_HOME, STATE_AWAY):
        raise ValueError(f"Invalid presence '{presence}' - must be '{STATE_HOME}' or '{STATE_AWAY}'")

    state = load_state()
    entry = {"state": presence, "event": event, "last_updated": _now()}
    state.setdefault("people", {})[person] = entry
    save_state(state)
    logger.debug("Presence: %s is now %s (event: %s)", person, presence, event)
    return entry


def get_state(person: str) -> Optional[Dict[str, Any]]:
    """Return the stored entry for a person, or ``None`` if never seen."""
    return load_state().get("people", {}).get(person)


def all_states() -> Dict[str, Any]:
    """Return the stored entries for everyone, keyed by person."""
    return load_state().get("people", {})


def anyone_home(overrides: Optional[Dict[str, str]] = None) -> bool:
    """Return True when at least one person in the store is home.

    ``overrides`` maps a person to a state to apply on top of the stored ones,
    so a caller can ask about the household *after* an event it has not written
    yet::

        anyone_home({person: STATE_AWAY})   # is anybody left once they leave?

    A store with nobody in it (and no overrides) counts as nobody home.
    """
    states = {name: entry.get("state") for name, entry in all_states().items()}
    if overrides:
        states.update(overrides)
    return any(state == STATE_HOME for state in states.values())


def _now() -> str:
    """Timestamp in the same format the logging engine uses."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


if __name__ == "__main__":
    # Simple smoke test / demo — writes to state/presence.json.
    print("resolve_person({'id': 'Alex'}) ->", resolve_person({"id": "Alex"}))
    print("resolve_person({})             ->", resolve_person({}))
    print("set_state ->", set_state(resolve_person({}), STATE_AWAY, event="leaving_home"))
    print("get_state ->", get_state(DEFAULT_PERSON))
    print("anyone_home ->", anyone_home())
    print("Wrote to:", STATE_FILE)
