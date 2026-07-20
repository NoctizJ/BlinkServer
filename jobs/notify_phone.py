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

The title/message (and the arm/disarm flags) for each event are configurable
in notify_config.json. An incoming webhook payload may also override "title",
"message", and the "arm"/"disarm" flag per request. Resolution precedence is:
payload > notify_config.json > built-in default.
"""

import os
import json
import logging
from typing import Dict, Any

try:
    # Shared Home Assistant wrappers + logging engine.
    from jobs.home_assistant_notify import notify_phone
    from jobs.home_assistant_arm_disarm import set_alarm
    from jobs.log_engine import log as write_log
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_notify import notify_phone
    from home_assistant_arm_disarm import set_alarm
    from log_engine import log as write_log

logger = logging.getLogger(__name__)

NOTIFY_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "notify_config.json")

# The alarm action associated with each event, and its config-flag key.
EVENT_ACTIONS = {"leaving_home": "arm", "arriving_home": "disarm"}

# Fallbacks used when notify_config.json is missing or lacks an event's entry.
DEFAULT_MESSAGES = {
    "leaving_home": {"title": "Leaving home", "message": "You have left home.", "arm": True},
    "arriving_home": {"title": "Arriving home", "message": "You have arrived home.", "disarm": True},
}


def _load_event_config(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the title/message and arm/disarm flag for an event.

    Precedence: request payload > notify_config.json > built-in default.
    """
    defaults = DEFAULT_MESSAGES.get(event, {"title": "Notification", "message": ""})
    action = EVENT_ACTIONS.get(event)  # "arm", "disarm", or None

    file_cfg: Dict[str, Any] = {}
    try:
        with open(NOTIFY_CONFIG_FILE, "r") as f:
            file_cfg = json.load(f).get(event, {})
    except FileNotFoundError:
        logger.warning("notify_config.json not found; using defaults for %s", event)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in notify_config.json: %s", e)

    payload = payload if isinstance(payload, dict) else {}
    resolved: Dict[str, Any] = {
        "title": payload.get("title") or file_cfg.get("title") or defaults["title"],
        "message": payload.get("message") or file_cfg.get("message") or defaults["message"],
    }

    # Arm/disarm flag: first source that mentions it wins (payload > file >
    # default). Checked by membership so an explicit `false` is honored and
    # not treated as "unset".
    if action:
        for source in (payload, file_cfg, defaults):
            if action in source:
                resolved[action] = bool(source[action])
                break
        else:
            resolved[action] = False

    return resolved


def _run_event(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Optionally arm/disarm the panel, then send the phone notification."""
    cfg = _load_event_config(event, payload)
    action = EVENT_ACTIONS.get(event)  # "arm" or "disarm"
    result: Dict[str, Any] = {"event": event}

    # 1. Arm (leaving) or disarm (arriving) the alarm panel, gated by config.
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
        write_log("blink", f"NOTIFY {event} ERROR: {error_msg}")
        result["notify"] = {"status": "error", "error": "Notification failed", "message": error_msg}

    write_log(
        "blink",
        f"NOTIFY {event}: notify={result['notify'].get('status')} "
        f"alarm={result['alarm'].get('status')} - {cfg['title']}",
    )
    return result


def leaving_home(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Webhook handler: arm the panel (if enabled) and notify you're leaving home."""
    logger.debug("leaving_home payload: %s", payload)
    return _run_event("leaving_home", payload)


def arriving_home(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Webhook handler: disarm the panel (if enabled) and notify you're arriving home."""
    logger.debug("arriving_home payload: %s", payload)
    return _run_event("arriving_home", payload)


if __name__ == "__main__":
    # Manual smoke test — requires a valid home_assistant_config.json.
    print("Testing notifyPhone job...")
    print(f"leaving_home:  {leaving_home({})}")
    print(f"arriving_home: {arriving_home({})}")
    print(f"payload override: {leaving_home({'title': 'Custom', 'message': 'Overridden'})}")
