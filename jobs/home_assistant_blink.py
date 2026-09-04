#!/usr/bin/env python3
"""
Blink alarm panel control, through Home Assistant.

This module arms and disarms the Blink alarm panel via Home Assistant, and is the
single place any panel call is made from.

The two webhook handlers (``arm`` -> /webhook/blink/arm, ``disarm`` ->
/webhook/blink/disarm) also push a phone notification, like the other notifying
jobs do. Their titles and messages come from **configs/notify_config.json**
under the ``blink_arm`` and ``blink_disarm`` events, falling back to
:data:`DEFAULT_BLINK_NOTIFY`. A ``"{home}"`` in either is replaced with the house
whose panel was changed, so one title serves every home::

    "blink_arm": { "title": "Blink Control {home} (A)" }   -> "Blink Control M (A)"

Two switches gate that notification, the same two-level shape the logging engine
and the location notifier use (see :mod:`jobs.switches`):

  * the **master** switch — the ``notify_phone`` entry in
    ``configs/job_switches.json``, shared with every other phone notification,
    so ``POST /jobs/notify_phone/disable`` silences this too;
  * a **per-action** switch — ``arm`` and ``disarm`` in the ``blink_control``
    section of ``configs/notify_switches.json``::

        {
          "location_log": { "娜": true },
          "blink_control": { "arm": true, "disarm": false },
          "last_modified": "..."
        }

Both must be on for a notification to go out. Turning a switch off does NOT stop
the panel being armed or disarmed — it only silences the notification. Toggle
them over HTTP with ``POST /blink/notify/<action>/enable|disable|toggle``, and
list them with ``GET /blink/notify``.

Which panel is armed comes from ``blink.panel_<home>`` in
``configs/home_assistant_entities.json``, where ``<home>`` is the payload's
``home`` field (defaulting to ``DEFAULT_HOME``) — the same key the presence and
leaving/arriving webhooks use. A home with no panel entry raises rather than
arming a different house.

Above both sits the ``blink`` feature switch in
``configs/home_assistant_switches.json`` (see :mod:`jobs.home_assistant_switches`),
checked inside :func:`set_alarm`. Turning *that* off stops every panel call this
server makes — from ``/webhook/blink/*`` and from the leaving/arriving webhooks
alike — without touching the notifications.

The notification lives in the handlers rather than in :func:`set_alarm`, because
``set_alarm`` is shared with the leaving/arriving webhooks — putting it in the
core would send those two notifications per event instead of one.
"""

import os
import json
import logging
from typing import Dict, Any

import requests

try:
    # Logging engine — records arm/disarm events under the "blink" log type.
    from jobs.log_engine import log as write_log
    from jobs.home_assistant_entities import entity as ha_entity
    from jobs.presence_state import DEFAULT_HOME, resolve_home
    from jobs.home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from jobs.home_assistant_switches import BLINK as HA_BLINK_FEATURE
    from jobs.home_assistant_switches import enabled_for as ha_feature_enabled
    from jobs.home_assistant_switches import skipped as ha_feature_skipped
    from jobs.switches import REPO_ROOT, all_switches, is_enabled, master_enabled, set_enabled
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import log as write_log
    from home_assistant_entities import entity as ha_entity
    from presence_state import DEFAULT_HOME, resolve_home
    from home_assistant_notify import fill_placeholders, load_event_text, notify_phone
    from home_assistant_switches import BLINK as HA_BLINK_FEATURE
    from home_assistant_switches import enabled_for as ha_feature_enabled
    from home_assistant_switches import skipped as ha_feature_skipped
    from switches import REPO_ROOT, all_switches, is_enabled, master_enabled, set_enabled

logger = logging.getLogger(__name__)

# Where the alarm panel entity ids live in home_assistant_entities.json. The key
# carries the home name, using the same names as state/presence.json, so a second
# house adds "panel_M" beside "panel_AMS" rather than replacing it.
PANEL_FEATURE = "blink"
PANEL_KEY_PREFIX = "panel"

# notify_config.json event names holding each handler's notification text.
NOTIFY_EVENTS = {"arm": "blink_arm", "disarm": "blink_disarm"}

# The per-action notification switches: configs/notify_switches.json, shared with
# the location notifier's own section in the same file.
SWITCH_FILE = REPO_ROOT / "configs" / "notify_switches.json"
SWITCH_SECTION = "blink_control"

# The job whose job_switches.json entry is the master switch for every phone
# notification, shared with jobs/notify_phone.py and jobs/location_notify.py.
MASTER_SWITCH = "notify_phone"

# Placeholder in a blink title/message replaced with the home's name. There is no
# person involved in arming a panel, so "{id}" is deliberately not offered.
HOME_PLACEHOLDER = "home"

# Fallbacks when notify_config.json is missing or lacks the entry.
DEFAULT_BLINK_NOTIFY = {
    "arm": {"title": "Blink Control {home} (A)", "message": ""},
    "disarm": {"title": "Blink Control {home} (D)", "message": ""},
}


def notify_enabled_for(action: str) -> bool:
    """Return whether ``action``'s notification is on, registering new actions."""
    return is_enabled(SWITCH_FILE, SWITCH_SECTION, action)


def set_notify_enabled_for(action: str, enabled: bool) -> bool:
    """Turn ``action``'s notification on or off; returns the value written."""
    logger.info("Blink %s notification %s", action, "enabled" if enabled else "disabled")
    return set_enabled(SWITCH_FILE, SWITCH_SECTION, action, enabled)


def all_notify_enabled() -> Dict[str, bool]:
    """Return both actions' notification switches, keyed by action."""
    return all_switches(SWITCH_FILE, SWITCH_SECTION)


def _notify_blink(action: str, home: str = DEFAULT_HOME) -> Dict[str, Any]:
    """Send the phone notification for a /webhook/blink/<action> request.

    Gated by the master ``notify_phone`` switch and this action's own switch; a
    switch that is off yields a ``"skipped"`` result and no request. The
    title/message come from notify_config.json's ``blink_arm`` / ``blink_disarm``
    entry, falling back to :data:`DEFAULT_BLINK_NOTIFY`, and a "{home}" in either
    is replaced with the house whose panel was just changed. Problems are reported
    in the return value rather than raised, so a broken notification never fails
    the arm/disarm call.
    """
    if not master_enabled(MASTER_SWITCH):
        return {"status": "skipped",
                "message": f"phone notifications are off (master '{MASTER_SWITCH}' switch)"}
    if not notify_enabled_for(action):
        return {"status": "skipped",
                "message": f"blink {action} notifications are off"}

    defaults = DEFAULT_BLINK_NOTIFY[action]
    cfg = load_event_text(NOTIFY_EVENTS[action])
    values = {HOME_PLACEHOLDER: home}
    title = fill_placeholders(cfg.get("title") or defaults["title"], values)
    message = fill_placeholders(cfg.get("message") or defaults["message"], values)

    try:
        return notify_phone(title, message)
    except Exception as e:
        error_msg = str(e)
        logger.error("blink %s notification failed: %s", action, error_msg)
        write_log("blink", f"{action.upper()} NOTIFY ERROR: {error_msg}")
        return {"status": "error", "error": "Notification failed", "message": error_msg}

def load_config() -> Dict[str, str]:
    """
    Load the Home Assistant connection settings.

    Returns:
        dict: Configuration dictionary with HA_BASE_URL and HA_API_KEY. The panel
        entity is no longer required here — see :func:`panel_entity`.
    """
    config_file = os.path.join(os.path.dirname(__file__), "..", "configs", "home_assistant_config.json")

    try:
        with open(config_file, 'r') as f:
            config = json.load(f)

        # Validate required fields
        required_fields = ["HA_BASE_URL", "HA_API_KEY"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Missing required configuration field: {field}")

        return config

    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {config_file}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration file: {e}")
    except Exception as e:
        raise ValueError(f"Error loading configuration: {e}")


def panel_entity(home: str = DEFAULT_HOME) -> str:
    """Return a home's alarm panel entity id, from home_assistant_entities.json.

    Args:
        home: Which house, defaulting to ``DEFAULT_HOME``. Every home currently
            shares one panel, so :func:`set_alarm` does not pass this yet — the
            argument is here so per-home panels are a one-line change once a
            second ``panel_<home>`` exists.

    Raises:
        ValueError: that home has no panel configured.
    """
    key = f"{PANEL_KEY_PREFIX}_{home}"
    entity_id = ha_entity(PANEL_FEATURE, key)
    if not entity_id:
        raise ValueError(
            f"No alarm panel configured for home '{home}': set '{key}' under "
            f"'{PANEL_FEATURE}' in configs/home_assistant_entities.json"
        )
    return entity_id

def set_alarm(action: str, home: str = DEFAULT_HOME) -> Dict[str, Any]:
    """Arm or disarm a home's Home Assistant alarm panel.

    This is the reusable core of the arm/disarm job — other jobs (e.g. the
    notifyPhone leaving/arriving handlers) import it directly. Being the single
    point every panel call goes through, the ``blink`` feature switch in
    ``home_assistant_switches.json`` is checked here and covers every caller.

    Args:
        action: "arm" or "disarm".
        home: Which house, defaulting to ``DEFAULT_HOME``. Its panel comes from
            ``blink.panel_<home>`` in home_assistant_entities.json, so a home
            with no entry raises rather than arming the wrong house.

    Returns:
        dict: {"status": "success"|"error"|"skipped", ...} describing the outcome.
        HTTP failures are reported in the return value; an invalid action, an
        unconfigured home, or missing configuration raises ValueError. A
        ``"skipped"`` result means the ``blink`` feature is switched off — no
        request was made and the configuration was not read.
    """
    if action not in ("arm", "disarm"):
        raise ValueError(f"Invalid action '{action}' - must be either 'arm' or 'disarm'")

    # Checked before the config is loaded, so the panel can be switched off
    # without home_assistant_config.json being present or valid.
    if not ha_feature_enabled(HA_BLINK_FEATURE):
        logger.info("%s skipped: Home Assistant '%s' is off", action, HA_BLINK_FEATURE)
        return ha_feature_skipped(HA_BLINK_FEATURE)

    # Load configuration from file
    config = load_config()
    ha_base_url = config["HA_BASE_URL"].rstrip("/")
    ha_api_key = config["HA_API_KEY"]
    ha_entity_id = panel_entity(home)

    # Debug logging for configuration
    logger.debug("Configuration loaded - Base URL: %s, Entity ID: %s (home %s)",
                 ha_base_url, ha_entity_id, home)

    # Construct the correct API endpoint for Home Assistant
    if action == "arm":
        api_endpoint = f"{ha_base_url}/api/services/alarm_control_panel/alarm_arm_away"
    else:
        api_endpoint = f"{ha_base_url}/api/services/alarm_control_panel/alarm_disarm"

    # Debug logging for API endpoint
    logger.debug("API endpoint for %s: %s", action, api_endpoint)

    # Prepare headers
    headers = {
        "Authorization": f"Bearer {ha_api_key}",
        "Content-Type": "application/json"
    }

    # Prepare data payload for Home Assistant
    data = {
        "entity_id": ha_entity_id
    }

    # Debug logging for request data
    logger.debug("Request headers: %s", headers)
    logger.debug("Request data: %s", data)

    # Make the API call to Home Assistant
    logger.debug("Making API request to %s", api_endpoint)

    response = requests.post(
        api_endpoint,
        headers=headers,
        json=data,
        timeout=30
    )

    # Debug logging for response
    logger.debug("API Response Status: %d", response.status_code)
    logger.debug("API Response Text: %s", response.text)

    # Check if the request was successful
    if response.status_code == 200:
        message = f"Successfully {action}ed the system"
        logger.info(message)
        write_log("blink", f"{action.upper()} event: {message} (entity: {ha_entity_id})")
        return {
            "message": message,
            "status": "success"
        }
    else:
        error_msg = f"HTTP {response.status_code}: {response.text}"
        logger.error(f"Failed to {action} system: {error_msg}")
        write_log("blink", f"{action.upper()} event FAILED: {error_msg} (entity: {ha_entity_id})")
        return {
            "error": f"Failed to {action} system",
            "message": f"Error occurred while trying to {action} the system: {error_msg}",
            "status": "error"
        }


def _run_blink_action(action: str, payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Arm/disarm a home's panel and notify the phone, whatever the panel did.

    The house comes from the payload's "home" field, defaulting to
    ``DEFAULT_HOME`` — the same key and the same default the presence and
    leaving/arriving webhooks use.

    The notification is attempted even when the panel call fails or raises, so a
    broken or unreachable panel still tells you the webhook fired.
    """
    home = resolve_home(payload)
    try:
        result = set_alarm(action, home=home)
    except Exception as e:
        error_msg = str(e)
        logger.error("%s failed for home %s: %s", action, home, error_msg)
        write_log("blink", f"{action.upper()} event ERROR (home {home}): {error_msg}")
        result = {
            "status": "error",
            "error": f"Failed to {action} system",
            "message": error_msg,
        }
    result["home"] = home
    result["notify"] = _notify_blink(action, home)
    return result


def arm(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/blink/arm — arm the alarm panel.

    The intent comes from the route, so the only optional field is ``home``,
    naming which house's panel to arm (defaults to ``DEFAULT_HOME``). A phone
    notification is sent as well, whatever the panel call returned.
    """
    logger.debug("arm handler payload: %s", payload)
    return _run_blink_action("arm", payload)


def disarm(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/blink/disarm — disarm the alarm panel.

    The intent comes from the route, so the only optional field is ``home``,
    naming which house's panel to disarm (defaults to ``DEFAULT_HOME``). A phone
    notification is sent as well, whatever the panel call returned.
    """
    logger.debug("disarm handler payload: %s", payload)
    return _run_blink_action("disarm", payload)


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute arm or disarm based on an ``action`` field in the payload.

    Retained for backward compatibility; the dedicated ``arm``/``disarm``
    handlers above are the preferred entry points and need no payload.

    Args:
        payload (dict): The request payload containing the action

    Returns:
        dict: Response with success/error message
    """
    # Debug logging for incoming payload
    logger.debug("Received payload: %s", payload)

    try:
        # Validate payload
        if not isinstance(payload, dict):
            error_msg = "Invalid payload format - Payload must be a JSON object"
            logger.error(error_msg)
            return {
                "error": "Invalid payload format",
                "message": error_msg
            }

        # Check for required 'action' field
        if "action" not in payload:
            error_msg = "Missing action field in payload"
            logger.error(error_msg)
            return {
                "error": "Missing action",
                "message": error_msg
            }

        action = payload["action"]

        # Validate action value
        if action not in ["arm", "disarm"]:
            error_msg = f"Invalid action '{action}' - must be either 'arm' or 'disarm'"
            logger.error(error_msg)
            return {
                "error": "Invalid action",
                "message": error_msg
            }

        # Delegate to the reusable arm/disarm core.
        return set_alarm(action, home=resolve_home(payload))

    except Exception as e:
        error_msg = str(e)
        action_label = payload.get("action", "unknown") if isinstance(payload, dict) else "unknown"
        logger.error(f"Unexpected error during {action_label} operation: {error_msg}")
        write_log("blink", f"{action_label.upper()} event ERROR: {error_msg}")
        return {
            "error": "Operation failed",
            "message": f"Error occurred while trying to {action_label} the system: {error_msg}",
            "status": "error"
        }

if __name__ == "__main__":
    # Test the function with sample payloads
    print("Testing Home Assistant arm/disarm functionality...")

    # Test arm
    result = run({"action": "arm"})
    print(f"Arm test: {result}")

    # Test disarm
    result = run({"action": "disarm"})
    print(f"Disarm test: {result}")

    # Test invalid payload
    result = run("invalid")
    print(f"Invalid payload test: {result}")

    # Test missing action
    result = run({})
    print(f"Missing action test: {result}")

    print("Testing completed!")