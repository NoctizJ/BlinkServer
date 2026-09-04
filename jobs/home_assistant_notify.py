#!/usr/bin/env python3
"""Home Assistant phone-notification helper for Blink Server.

This is the reusable notification plumbing: a single Home Assistant `notify`
service call, plus the title/message configuration every notifying job shares.
Any job can import notify_phone() to push a notification (title + message) to a
phone running the Home Assistant app.

The service call mirrors this request:

    curl -X POST \\
      -H "Authorization: Bearer <YOUR_TOKEN>" \\
      -H "Content-Type: application/json" \\
      http://<hostID>:8123/api/services/notify/<notify target> \\
      -d '{"title": "...", "message": "..."}'

Titles and messages come from **configs/notify_config.json**, keyed by event
(``leaving_home``, ``arriving_home``, ``location_log``, ...) — read one with
:func:`load_event_text` and fill its ``{id}``-style placeholders with
:func:`fill_placeholders`. Who uses which event is up to the job:
:mod:`jobs.notify_phone` owns the arrival/departure events and
:mod:`jobs.location_notify` the location one.
"""

import os
import json
import logging
from typing import Dict, Any

import requests

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.home_assistant_entities import entity as ha_entity
    from jobs.home_assistant_switches import NOTIFY as HA_NOTIFY_FEATURE
    from jobs.home_assistant_switches import enabled_for as ha_feature_enabled
    from jobs.home_assistant_switches import skipped as ha_feature_skipped
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_entities import entity as ha_entity
    from home_assistant_switches import NOTIFY as HA_NOTIFY_FEATURE
    from home_assistant_switches import enabled_for as ha_feature_enabled
    from home_assistant_switches import skipped as ha_feature_skipped

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "home_assistant_config.json")

NOTIFY_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "notify_config.json")

# Where the notify service target lives in home_assistant_entities.json. One
# phone is shared by every home, so this key carries no home name.
TARGET_FEATURE = "notify"
TARGET_KEY = "target"


def load_event_text(event: str) -> Dict[str, Any]:
    """Return an event's entry from notify_config.json, or ``{}``.

    A missing file or a missing event is not an error — the calling job falls
    back to its own built-in title/message. Unreadable JSON is logged and treated
    the same way, so a typo in the config cannot cost you the notification.
    """
    try:
        with open(NOTIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
            entry = json.load(f).get(event, {})
    except FileNotFoundError:
        logger.warning("notify_config.json not found; using defaults for %s", event)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in notify_config.json: %s", e)
        return {}
    return entry if isinstance(entry, dict) else {}


def fill_placeholders(text: Any, values: Dict[str, Any]) -> str:
    """Replace ``{key}`` in text with ``values[key]``, for every key given.

    Done with plain replacement rather than str.format() so stray braces in a
    configured message can never raise, and so an unknown placeholder is left
    visible in the notification instead of blowing up.
    """
    text = str(text)
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text


def _load_ha_config() -> Dict[str, str]:
    """Load Home Assistant connection settings for phone notifications.

    The same home_assistant_config.json the other Home Assistant jobs use. The
    notify service target is no longer read from here — see
    :func:`notify_target`.
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {CONFIG_FILE}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration file: {e}")

    required_fields = ["HA_BASE_URL", "HA_API_KEY"]
    for field in required_fields:
        if not config.get(field):
            raise ValueError(f"Missing required configuration field: {field}")

    return config


def notify_target() -> str:
    """Return the Home Assistant notify service target for the phone.

    Read from ``home_assistant_entities.json`` (``notify.target``).

    Raises:
        ValueError: the target is not configured.
    """
    target = ha_entity(TARGET_FEATURE, TARGET_KEY)
    if not target:
        raise ValueError(
            f"No notify target configured: set '{TARGET_KEY}' under "
            f"'{TARGET_FEATURE}' in configs/home_assistant_entities.json"
        )
    return target


def notify_phone(title: str, message: str) -> Dict[str, Any]:
    """Send a notification to the configured phone via Home Assistant.

    This is the single point every phone notification goes through, so the
    ``notify`` feature switch in ``home_assistant_switches.json`` is checked here
    and covers every caller at once.

    Args:
        title: The notification title.
        message: The notification body.

    Returns:
        dict: {"status": "success"|"error"|"skipped", ...} describing the outcome.
        HTTP failures are reported in the return value rather than raised;
        configuration errors (missing file/fields) still raise ValueError. A
        ``"skipped"`` result means the ``notify`` feature is switched off — no
        request was made and the configuration was not read.
    """
    if not ha_feature_enabled(HA_NOTIFY_FEATURE):
        logger.debug("notify_phone skipped: Home Assistant '%s' is off", HA_NOTIFY_FEATURE)
        return ha_feature_skipped(HA_NOTIFY_FEATURE)

    config = _load_ha_config()
    base_url = config["HA_BASE_URL"].rstrip("/")
    api_key = config["HA_API_KEY"]
    target = notify_target()

    endpoint = f"{base_url}/api/services/notify/{target}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {"title": title, "message": message}

    logger.debug("Sending notification to %s: %s", endpoint, data)

    response = requests.post(endpoint, headers=headers, json=data, timeout=30)

    logger.debug("Notify response status: %d, body: %s", response.status_code, response.text)

    # Home Assistant returns 200 (and 201 for some services) on success.
    if response.status_code in (200, 201):
        success_msg = f"Notification sent: {title}"
        logger.info(success_msg)
        return {"status": "success", "message": success_msg}

    error_msg = f"HTTP {response.status_code}: {response.text}"
    logger.error("Failed to send notification: %s", error_msg)
    return {
        "status": "error",
        "error": "Failed to send notification",
        "message": error_msg,
    }


if __name__ == "__main__":
    # Manual smoke test — requires a valid home_assistant_config.json.
    print("Sending test notification via Home Assistant...")
    print(notify_phone("Blink Server", "Test notification from home_assistant_notify.py"))
