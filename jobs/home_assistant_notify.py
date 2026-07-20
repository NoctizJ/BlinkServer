#!/usr/bin/env python3
"""Home Assistant phone-notification helper for Blink Server.

This is the reusable wrapper around a single Home Assistant `notify` service
call. Any job can import notify_phone() to push a notification (title +
message) to a phone running the Home Assistant app.

It mirrors this request:

    curl -X POST \\
      -H "Authorization: Bearer <YOUR_TOKEN>" \\
      -H "Content-Type: application/json" \\
      http://<hostID>:8123/api/services/notify/<HA_NOTIFY_TARGET> \\
      -d '{"title": "...", "message": "..."}'
"""

import os
import json
import logging
from typing import Dict, Any

import requests

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "home_assistant_config.json")


def _load_ha_config() -> Dict[str, str]:
    """Load Home Assistant connection settings for phone notifications.

    Reuses the same home_assistant_config.json as the arm/disarm job, but
    requires an extra HA_NOTIFY_TARGET field naming the notify service target
    (e.g. "mobile_app_aisingioro").
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {CONFIG_FILE}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration file: {e}")

    required_fields = ["HA_BASE_URL", "HA_API_KEY", "HA_NOTIFY_TARGET"]
    for field in required_fields:
        if not config.get(field):
            raise ValueError(f"Missing required configuration field: {field}")

    return config


def notify_phone(title: str, message: str) -> Dict[str, Any]:
    """Send a notification to the configured phone via Home Assistant.

    Args:
        title: The notification title.
        message: The notification body.

    Returns:
        dict: {"status": "success"|"error", ...} describing the outcome. HTTP
        failures are reported in the return value rather than raised;
        configuration errors (missing file/fields) still raise ValueError.
    """
    config = _load_ha_config()
    base_url = config["HA_BASE_URL"].rstrip("/")
    api_key = config["HA_API_KEY"]
    target = config["HA_NOTIFY_TARGET"]

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
