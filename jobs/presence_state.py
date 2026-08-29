#!/usr/bin/env python3
"""Presence state store for Blink Server.

Persists, per home and per person, whether they are currently home or away. The
state lives in a single JSON file (``state/presence.json``) so it survives
restarts:

    {
      "homes": {
        "A": {
          "people": {
            "娜": {
              "state": "home",
              "event": "arriving_home",
              "last_updated": "2026-08-03 18:42:11.482"
            }
          }
        },
        "M": {
          "people": {
            "Sam": {"state": "away", "event": "leaving_home", "last_updated": "..."}
          }
        }
      },
      "last_modified": "2026-08-03 18:42:11.482"
    }

Two things identify an entry, both taken from the webhook payload:

  * ``id``   — who this is; a post without one is attributed to ``DEFAULT_PERSON``
  * ``home`` — which house; a post without one is attributed to ``DEFAULT_HOME``

Homes are independent namespaces: "Sam" in home ``A`` and "Sam" in home ``M`` are
two separate entries, and one can be home while the other is away.

Older single-home files — a top-level ``people`` map with no ``homes`` — are
migrated into ``DEFAULT_HOME`` when they are read, and the new shape is written
out by the next save. Nothing has to be converted by hand.

Usage:
    from jobs.presence_state import resolve_home, resolve_person, set_state

    person = resolve_person(payload)      # payload["id"]   or "娜"
    home = resolve_home(payload)          # payload["home"] or "A"
    set_state(person, STATE_AWAY, event="leaving_home", home=home)
    get_state(person, home=home)          # -> {"state": "away", ...}
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

# Payload key naming the house the event belongs to.
HOME_KEY = "home"

# Attribution for posts that omit the id.
DEFAULT_PERSON = "娜"

# Attribution for posts that omit the home, so a single-home setup never has to
# mention one.
DEFAULT_HOME = "A"

# Reserved home name asking a reader for every home at once.
ALL_HOMES = "all"

# The two presence states.
STATE_HOME = "home"
STATE_AWAY = "away"


def _resolve_key(payload: Optional[Dict[str, Any]], key: str, fallback: str) -> str:
    """Return a stripped scalar ``payload[key]``, or ``fallback``.

    Shared by :func:`resolve_person` and :func:`resolve_home` so ``id`` and
    ``home`` behave identically: missing, blank, or non-scalar falls back.
    """
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return fallback


def resolve_person(payload: Optional[Dict[str, Any]]) -> str:
    """Return the person named by ``payload["id"]``, or ``DEFAULT_PERSON``.

    A missing, non-scalar, or blank ``id`` falls back to ``DEFAULT_PERSON``, so
    an unlabelled webhook post is still attributed to somebody.
    """
    return _resolve_key(payload, ID_KEY, DEFAULT_PERSON)


def resolve_home(payload: Optional[Dict[str, Any]]) -> str:
    """Return the home named by ``payload["home"]``, or ``DEFAULT_HOME``.

    Mirrors :func:`resolve_person`, so a post that names neither an ``id`` nor a
    ``home`` lands on the default person in the default home — which is exactly
    how this server behaved before it knew about more than one house.
    """
    return _resolve_key(payload, HOME_KEY, DEFAULT_HOME)


def _empty_state() -> Dict[str, Any]:
    """A store with no homes in it."""
    return {"homes": {}, "last_modified": None}


def load_state() -> Dict[str, Any]:
    """Load the whole presence file, falling back to an empty store.

    A legacy single-home file (top-level ``people``, no ``homes``) is returned
    reshaped into ``DEFAULT_HOME``; the reshape reaches disk on the next
    :func:`save_state`. The conversion is read-side and idempotent, so it needs
    no migration script.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s (%s); starting from an empty store", STATE_FILE, e)
        return _empty_state()

    if not isinstance(state, dict):
        logger.error("Unexpected structure in %s; starting from an empty store", STATE_FILE)
        return _empty_state()

    if "homes" not in state and isinstance(state.get("people"), dict):
        logger.info("Migrating the single-home presence file into home '%s'", DEFAULT_HOME)
        return {
            "homes": {DEFAULT_HOME: {"people": state["people"]}},
            "last_modified": state.get("last_modified"),
        }

    if not isinstance(state.get("homes"), dict):
        logger.error("Unexpected structure in %s; starting from an empty store", STATE_FILE)
        return _empty_state()
    return state


def save_state(state: Dict[str, Any]) -> None:
    """Persist the presence file, stamping the modification time.

    ``ensure_ascii=False`` keeps non-ASCII home and person names (e.g. "娜")
    readable in the file rather than escaped as ``\\u5a1c``.
    """
    state["last_modified"] = _now()
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _people_in(state: Dict[str, Any], home: str) -> Dict[str, Any]:
    """Return one home's people map from a loaded store, or ``{}``.

    A home that does not exist, or one whose entry was hand-edited into an
    unexpected shape, reads as empty rather than raising.
    """
    house = state.get("homes", {}).get(home)
    if isinstance(house, dict) and isinstance(house.get("people"), dict):
        return house["people"]
    return {}


def set_state(
    person: str,
    presence: str,
    event: Optional[str] = None,
    home: str = DEFAULT_HOME,
) -> Dict[str, Any]:
    """Record that ``person`` is now home or away in ``home``, and persist it.

    The home is created on first write, so a new house needs no setup.

    Args:
        person: The person's name (see :func:`resolve_person`).
        presence: ``STATE_HOME`` or ``STATE_AWAY``.
        event: Optional event name that caused the change, e.g. "leaving_home".
        home: Which house (see :func:`resolve_home`).

    Returns:
        dict: The stored entry for that person in that home.
    """
    if presence not in (STATE_HOME, STATE_AWAY):
        raise ValueError(f"Invalid presence '{presence}' - must be '{STATE_HOME}' or '{STATE_AWAY}'")

    state = load_state()
    entry = {"state": presence, "event": event, "last_updated": _now()}

    homes = state.setdefault("homes", {})
    house = homes.get(home)
    if not isinstance(house, dict):
        house = {}
        homes[home] = house
    if not isinstance(house.get("people"), dict):
        house["people"] = {}
    house["people"][person] = entry

    save_state(state)
    logger.debug("Presence: %s is now %s in home %s (event: %s)", person, presence, home, event)
    return entry


def get_state(person: str, home: str = DEFAULT_HOME) -> Optional[Dict[str, Any]]:
    """Return a person's stored entry in a home, or ``None`` if never seen."""
    return _people_in(load_state(), home).get(person)


def all_states(home: str = DEFAULT_HOME) -> Dict[str, Any]:
    """Return the stored entries for everyone in one home, keyed by person."""
    return _people_in(load_state(), home)


def all_homes() -> Dict[str, Dict[str, Any]]:
    """Return every home's people map, keyed by home name and sorted by it."""
    state = load_state()
    return {home: _people_in(state, home) for home in sorted(state.get("homes", {}))}


def anyone_home(overrides: Optional[Dict[str, str]] = None, home: str = DEFAULT_HOME) -> bool:
    """Return True when at least one person in ``home`` is home.

    ``overrides`` maps a person to a state to apply on top of the stored ones,
    so a caller can ask about the household *after* an event it has not written
    yet::

        anyone_home({person: STATE_AWAY}, home=home)   # anybody left once they go?

    Only the named home is considered — somebody being home in another house
    does not count. A home with nobody in it (and no overrides) counts as
    nobody home.
    """
    states = {name: entry.get("state") for name, entry in all_states(home).items()}
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
    print("resolve_home({'home': 'M'})    ->", resolve_home({"home": "M"}))
    print("resolve_home({})               ->", resolve_home({}))
    print("set_state (default home) ->", set_state(resolve_person({}), STATE_AWAY, event="leaving_home"))
    print("set_state (home M)       ->", set_state("Sam", STATE_HOME, event="arriving_home", home="M"))
    print("get_state (default home) ->", get_state(DEFAULT_PERSON))
    print("anyone_home('A')         ->", anyone_home())
    print("anyone_home('M')         ->", anyone_home(home="M"))
    print("all_homes                ->", all_homes())
    print("Wrote to:", STATE_FILE)
