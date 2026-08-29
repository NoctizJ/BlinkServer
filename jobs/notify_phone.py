#!/usr/bin/env python3
"""notifyPhone job — home arrival/departure notifications.

Two webhooks share this module (see config.json):

  - leaving_home(payload)  -> POST /webhook/notify/leaving
  - arriving_home(payload) -> POST /webhook/notify/arriving

Each webhook does two things:

  1. Optionally arms (leaving) or disarms (arriving) the Home Assistant alarm
     panel, via the shared jobs.home_assistant_arm_disarm.set_alarm() core.
     Whether it does so is controlled by a flag in notify_config.json
     ("arm" for leaving_home, "disarm" for arriving_home).
  2. Pushes a notification to the phone through the shared
     jobs.home_assistant_notify.notify_phone() wrapper.
  3. Records who left/arrived in the presence store
     (state/presence.json, via jobs.presence_state).

The person is taken from the payload's "id" field; a post without an "id" is
attributed to jobs.presence_state.DEFAULT_PERSON ("娜"). The house is taken from
the payload's "home" field; a post without a "home" is attributed to
jobs.presence_state.DEFAULT_HOME ("AMS"), so a single-home setup never has to
mention one. Titles and messages may contain a "{id}" (or "{name}")
placeholder, replaced with that person's name, and a "{home}" placeholder,
replaced with the house.

Every title also gets an arm/disarm postfix describing the household once this
event is applied — "(A)" when nobody is home any more, "(D)" when at least one
person still is::

    Leaving home (A)     # the last person left, so arm
    Leaving home (D)     # somebody is still in, so disarm

Only the event's own home is counted, so somebody being home in another house
never turns this one's postfix into "(D)".

The title/message (and the arm/disarm flags) for each event are configurable in
notify_config.json, optionally per home via a "homes" block inside the event::

    "leaving_home": {
      "title": "がいしゅつ",
      "message": "{id}さんが家を出ました。",
      "arm": false,
      "homes": {
        "M": {"title": "M — がいしゅつ", "arm": true}
      }
    }

Every key inside a "homes" entry is optional, so the example above leaves home M
inheriting the shared message. A home with no block behaves exactly like the
shared configuration. An incoming webhook payload may also override "title",
"message", and the "arm"/"disarm" flag per request. Resolution precedence is:
payload > the event's "homes" block for this home > notify_config.json >
built-in default.

All homes share one Home Assistant configuration, so they arm and disarm the
same panel and notify the same phone — only the text and the bookkeeping are
per-home.
"""

import logging
from typing import Dict, Any

try:
    # Shared Home Assistant wrappers + logging engine.
    from jobs.home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from jobs.home_assistant_arm_disarm import set_alarm
    from jobs.log_engine import log as write_log
    from jobs.presence_state import (
        DEFAULT_HOME,
        STATE_AWAY,
        STATE_HOME,
        anyone_home,
        resolve_home,
        resolve_person,
        set_state,
    )
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from home_assistant_arm_disarm import set_alarm
    from log_engine import log as write_log
    from presence_state import (
        DEFAULT_HOME,
        STATE_AWAY,
        STATE_HOME,
        anyone_home,
        resolve_home,
        resolve_person,
        set_state,
    )

logger = logging.getLogger(__name__)

# The alarm action associated with each event, and its config-flag key.
EVENT_ACTIONS = {"leaving_home": "arm", "arriving_home": "disarm"}

# The presence each event puts the person in.
EVENT_STATES = {"leaving_home": STATE_AWAY, "arriving_home": STATE_HOME}

# Placeholders in a title/message that are replaced with the person's name.
ID_PLACEHOLDERS = ("id", "name")

# Placeholder in a title/message that is replaced with the home's name.
HOME_PLACEHOLDER = "home"

# Section inside an event's notify_config.json entry holding per-home overrides.
HOMES_KEY = "homes"

# Postfix appended to the notification title, describing the household once
# this event is applied: "(A)" for arm (everybody is away), "(D)" for disarm (at
# least one person is still home).
POSTFIX_ARM = "(A)"
POSTFIX_DISARM = "(D)"

# Fallbacks used when notify_config.json is missing or lacks an event's entry.
DEFAULT_MESSAGES = {
    "leaving_home": {"title": "Leaving home", "message": "{id} has left home.", "arm": True},
    "arriving_home": {"title": "Arriving home", "message": "{id} has arrived home.", "disarm": True},
}


def _home_overrides(file_cfg: Dict[str, Any], home: str) -> Dict[str, Any]:
    """Return the per-home override block for a home, or ``{}``.

    A missing "homes" section, a home with no entry in it, or either one shaped
    unexpectedly by a hand edit all read as "no overrides", so the event falls
    back to its shared configuration instead of failing.
    """
    homes = file_cfg.get(HOMES_KEY)
    if not isinstance(homes, dict):
        return {}
    overrides = homes.get(home)
    return overrides if isinstance(overrides, dict) else {}


def _title_postfix(event: str, person: str, home: str = DEFAULT_HOME) -> str:
    """Return "(A)" or "(D)" for the household this event leaves behind.

    The person's new state (away for leaving, home for arriving) is applied on
    top of the stored presence *before* the check, so the postfix describes how
    things stand after the event even though the store is only written later —
    and stays right if that write fails.

    Only ``home`` is considered, so another house being occupied cannot make
    this one look occupied.

    "(A)" means arm: nobody is home any more. "(D)" means disarm: at least one
    person is still home. An event with no presence of its own gets no postfix.
    """
    presence = EVENT_STATES.get(event)
    if presence is None:
        return ""
    try:
        return POSTFIX_DISARM if anyone_home({person: presence}, home=home) else POSTFIX_ARM
    except Exception as e:  # An unreadable store must not cost us the notification.
        logger.error("could not read presence for the %s title postfix: %s", event, e)
        return ""


def _fill(text: Any, person: str, home: str) -> str:
    """Fill the "{id}"/"{name}" and "{home}" placeholders in a title or message."""
    values = {name: person for name in ID_PLACEHOLDERS}
    values[HOME_PLACEHOLDER] = home
    return fill_placeholders(text, values)


def _load_event_config(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the title/message and arm/disarm flag for an event.

    Precedence: request payload > the event's per-home override block > the
    event's shared notify_config.json entry > built-in default. The resolved
    title/message have their "{id}" and "{home}" placeholders filled in, and the
    title carries the "(A)"/"(D)" postfix from :func:`_title_postfix`.
    """
    defaults = DEFAULT_MESSAGES.get(event, {"title": "Notification", "message": ""})
    action = EVENT_ACTIONS.get(event)  # "arm", "disarm", or None

    file_cfg = load_event_text(event)

    payload = payload if isinstance(payload, dict) else {}
    person = resolve_person(payload)
    home = resolve_home(payload)
    home_cfg = _home_overrides(file_cfg, home)

    title = _fill(
        payload.get("title") or home_cfg.get("title") or file_cfg.get("title") or defaults["title"],
        person,
        home,
    )
    # Whatever the title's source, it carries the arm/disarm postfix.
    postfix = _title_postfix(event, person, home)
    resolved: Dict[str, Any] = {
        "person": person,
        "home": home,
        "postfix": postfix,
        "title": f"{title} {postfix}".strip() if postfix else title,
        "message": _fill(
            payload.get("message")
            or home_cfg.get("message")
            or file_cfg.get("message")
            or defaults["message"],
            person,
            home,
        ),
    }

    # Arm/disarm flag: first source that mentions it wins (payload > this home's
    # block > file > default). Checked by membership so an explicit `false` is
    # honored and not treated as "unset".
    if action:
        for source in (payload, home_cfg, file_cfg, defaults):
            if action in source:
                resolved[action] = bool(source[action])
                break
        else:
            resolved[action] = False

    return resolved


def _run_event(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Optionally arm/disarm the panel, notify the phone, record the presence."""
    cfg = _load_event_config(event, payload)
    person = cfg["person"]
    home = cfg["home"]
    action = EVENT_ACTIONS.get(event)  # "arm" or "disarm"
    result: Dict[str, Any] = {
        "event": event,
        "person": person,
        "home": home,
        "title": cfg["title"],
    }

    # 1. Arm (leaving) or disarm (arriving) the alarm panel, gated by config.
    #    Every home shares one panel, so this is not scoped by home.
    if action and cfg.get(action):
        try:
            result["alarm"] = set_alarm(action)
        except Exception as e:
            error_msg = str(e)
            logger.error("%s failed for %s: %s", action, event, error_msg)
            write_log("blink", f"NOTIFY {event} {action.upper()} ERROR: {error_msg}")
            result["alarm"] = {"status": "error", "error": f"{action} failed", "message": error_msg}
    else:
        result["alarm"] = {
            "status": "skipped",
            "message": f"{action or 'alarm'} disabled in config for {event}",
        }

    # 2. Send the phone notification (attempted regardless of the alarm result).
    try:
        result["notify"] = notify_phone(cfg["title"], cfg["message"])
    except Exception as e:
        error_msg = str(e)
        logger.error("notify_phone failed for %s: %s", event, error_msg)
        write_log("blink", f"NOTIFY {event} ({person}@{home}) ERROR: {error_msg}")
        result["notify"] = {"status": "error", "error": "Notification failed", "message": error_msg}

    # 3. Persist whether this person is now home or away, in their home.
    presence = EVENT_STATES.get(event)
    if presence:
        try:
            result["presence"] = set_state(person, presence, event=event, home=home)
        except Exception as e:
            error_msg = str(e)
            logger.error(
                "presence update failed for %s (%s@%s): %s", event, person, home, error_msg
            )
            write_log("blink", f"NOTIFY {event} ({person}@{home}) PRESENCE ERROR: {error_msg}")
            result["presence"] = {
                "status": "error",
                "error": "Presence update failed",
                "message": error_msg,
            }

    write_log(
        "blink",
        f"NOTIFY {event}: person={person} home={home} "
        f"presence={result.get('presence', {}).get('state', 'unchanged')} "
        f"notify={result['notify'].get('status')} "
        f"alarm={result['alarm'].get('status')} - {cfg['title']}",
    )
    return result


def leaving_home(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Webhook handler: arm the panel (if enabled), notify, mark the person away.

    The person comes from the payload's "id" field, defaulting to "娜", and the
    house from its "home" field, defaulting to "AMS".
    """
    logger.debug("leaving_home payload: %s", payload)
    return _run_event("leaving_home", payload)


def arriving_home(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Webhook handler: disarm the panel (if enabled), notify, mark the person home.

    The person comes from the payload's "id" field, defaulting to "娜", and the
    house from its "home" field, defaulting to "AMS".
    """
    logger.debug("arriving_home payload: %s", payload)
    return _run_event("arriving_home", payload)


if __name__ == "__main__":
    # Manual smoke test — requires a valid home_assistant_config.json.
    print("Testing notifyPhone job...")
    print(f"leaving_home:  {leaving_home({})}")
    print(f"arriving_home: {arriving_home({})}")
    print(f"with id:       {leaving_home({'id': 'Alex'})}")
    print(f"with home:     {leaving_home({'id': 'Sam', 'home': 'M'})}")
    print(f"payload override: {leaving_home({'title': 'Custom', 'message': 'Overridden'})}")
    print(f"postfix (away):   {_title_postfix('leaving_home', 'nobody', DEFAULT_HOME)}")
