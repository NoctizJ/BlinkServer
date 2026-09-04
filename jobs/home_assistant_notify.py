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
      http://<hostID>:8123/api/services/notify/<device target> \\
      -d '{"title": "...", "message": "..."}'

Titles and messages come from **configs/notify_config.json**, keyed by event
(``leaving_home``, ``arriving_home``, ``location_log``, ...) — read one with
:func:`load_event_text` and fill its ``{id}``-style placeholders with
:func:`fill_placeholders`. Who uses which event is up to the job:
:mod:`jobs.notify_phone` owns the arrival/departure events and
:mod:`jobs.location_notify` the location one.

One notification goes to **every** device listed in ``notify.devices`` in
``configs/home_assistant_entities.json``. A device may opt out of a kind of
notification with a ``disable`` list naming groups from :data:`NOTIFY_GROUPS`
— hand-edited on the server, deliberately not controllable per request.
"""

import os
import json
import logging
from typing import Any, Dict, Optional

import requests

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.home_assistant_entities import section as ha_section
    from jobs.home_assistant_switches import NOTIFY as HA_NOTIFY_FEATURE
    from jobs.home_assistant_switches import enabled_for as ha_feature_enabled
    from jobs.home_assistant_switches import skipped as ha_feature_skipped
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_entities import section as ha_section
    from home_assistant_switches import NOTIFY as HA_NOTIFY_FEATURE
    from home_assistant_switches import enabled_for as ha_feature_enabled
    from home_assistant_switches import skipped as ha_feature_skipped

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "home_assistant_config.json")

NOTIFY_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "notify_config.json")

# Where the notify devices live in home_assistant_entities.json. One list is
# shared by every home, so these keys carry no home name.
TARGET_FEATURE = "notify"
DEVICES_KEY = "devices"

# The kinds of notification this server sends, so a device can opt out of a
# whole kind rather than naming individual events. Every event this server
# notifies for belongs to exactly one group.
NOTIFY_GROUPS = {
    "leaving_home": "home_presence",
    "arriving_home": "home_presence",
    "blink_arm": "blink_arm_status",
    "blink_disarm": "blink_arm_status",
    "location_log": "location_update",
}

# Every group name, for validating a "disable" list.
NOTIFY_GROUP_NAMES = tuple(dict.fromkeys(NOTIFY_GROUPS.values()))


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
    The notify devices are no longer read from here — see
    :func:`notify_devices`.
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


def notify_devices() -> list:
    """Return the configured notify devices, as ``[{"target", "disable"}, ...]``.

    Read from the ``notify.devices`` list in home_assistant_entities.json::

        "notify": {
          "devices": [
            {"target": "mobile_app_aisingioro"},
            {"target": "mobile_app_ipad", "disable": ["location_update"]}
          ]
        }

    A device with no ``disable`` gets everything. Malformed entries are dropped
    with a log line rather than taking every notification down, and an unknown
    group name is warned about — a typo there would silently disable nothing.

    Raises:
        ValueError: no usable device is configured.
    """
    raw = ha_section(TARGET_FEATURE).get(DEVICES_KEY)
    devices = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            logger.error("Ignoring a non-object entry in notify.devices: %r", entry)
            continue
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            logger.error("Ignoring a notify device with no target: %r", entry)
            continue
        disabled = entry.get("disable") or []
        if not isinstance(disabled, list):
            logger.error("Ignoring a non-list 'disable' on %s: %r", target, disabled)
            disabled = []
        for group in disabled:
            if group not in NOTIFY_GROUP_NAMES:
                logger.warning(
                    "%s disables unknown group %r - known groups are: %s",
                    target, group, ", ".join(NOTIFY_GROUP_NAMES),
                )
        devices.append({"target": target.strip(), "disable": list(disabled)})

    if not devices:
        raise ValueError(
            f"No notify devices configured: set '{DEVICES_KEY}' under "
            f"'{TARGET_FEATURE}' in configs/home_assistant_entities.json"
        )
    return devices


def _wants(device: Dict[str, Any], event: Optional[str]) -> bool:
    """Whether a device should receive a given event.

    An event with no group (or no event at all — a manual test notification) is
    always sent: only a known group can be opted out of.
    """
    group = NOTIFY_GROUPS.get(event or "")
    return group is None or group not in device["disable"]


def notify_phone(title: str, message: str, event: Optional[str] = None) -> Dict[str, Any]:
    """Send a notification to every configured device via Home Assistant.

    This is the single point every phone notification goes through, so the
    ``notify`` feature switch in ``home_assistant_switches.json`` is checked here
    and covers every caller at once.

    Args:
        title: The notification title.
        message: The notification body.
        event: Which notification this is (a ``notify_config.json`` event name).
            Used to skip devices whose ``disable`` list opts out of that event's
            group — see :data:`NOTIFY_GROUPS`. Omitted means "send to everything",
            so a manual test notification always goes out.

    Returns:
        dict: ``{"status": "success"|"error"|"skipped", ...}`` for the batch, plus
        a ``devices`` list with a per-device outcome. ``"success"`` means every
        device that wanted it got it; one failure makes the batch ``"error"`` while
        still attempting the rest. ``"skipped"`` means the feature is off, or every
        device has opted out of this group. HTTP failures are reported rather than
        raised; configuration errors still raise ValueError.
    """
    if not ha_feature_enabled(HA_NOTIFY_FEATURE):
        logger.debug("notify_phone skipped: Home Assistant '%s' is off", HA_NOTIFY_FEATURE)
        return ha_feature_skipped(HA_NOTIFY_FEATURE)

    config = _load_ha_config()
    base_url = config["HA_BASE_URL"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {config['HA_API_KEY']}",
        "Content-Type": "application/json",
    }
    data = {"title": title, "message": message}

    all_devices = notify_devices()
    wanted = [d for d in all_devices if _wants(d, event)]
    if not wanted:
        group = NOTIFY_GROUPS.get(event or "")
        return {
            "status": "skipped",
            "message": f"every device has {group!r} disabled",
            "devices": [],
        }

    results = []
    for device in wanted:
        target = device["target"]
        endpoint = f"{base_url}/api/services/notify/{target}"
        logger.debug("Sending notification to %s: %s", endpoint, data)
        try:
            response = requests.post(endpoint, headers=headers, json=data, timeout=30)
        except requests.RequestException as e:
            # One unreachable device must not stop the others.
            logger.error("Could not reach %s: %s", target, e)
            results.append({"target": target, "status": "error", "message": str(e)})
            continue

        logger.debug("%s -> %d %s", target, response.status_code, response.text)
        # Home Assistant returns 200 (and 201 for some services) on success.
        if response.status_code in (200, 201):
            results.append({"target": target, "status": "success"})
        else:
            error_msg = f"HTTP {response.status_code}: {response.text}"
            logger.error("Failed to notify %s: %s", target, error_msg)
            results.append({"target": target, "status": "error", "message": error_msg})

    failed = [r for r in results if r["status"] != "success"]
    sent = len(results) - len(failed)
    skipped = len(all_devices) - len(wanted)
    detail = f"{sent}/{len(wanted)} device(s)" + (f", {skipped} opted out" if skipped else "")

    if failed:
        message_text = f"Notified {detail}; {len(failed)} failed: " + \
                       "; ".join(f"{r['target']}: {r['message']}" for r in failed)
        logger.error("Failed to send notification: %s", message_text)
        return {
            "status": "error",
            "error": "Failed to send notification",
            "message": message_text,
            "devices": results,
        }

    success_msg = f"Notification sent to {detail}: {title}"
    logger.info(success_msg)
    return {"status": "success", "message": success_msg, "devices": results}


if __name__ == "__main__":
    # Manual smoke test — requires a valid home_assistant_config.json.
    print("Sending test notification via Home Assistant...")
    print(notify_phone("Blink Server", "Test notification from home_assistant_notify.py"))
