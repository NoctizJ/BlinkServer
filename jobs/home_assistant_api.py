#!/usr/bin/env python3
"""Shared Home Assistant REST API caller.

Every Home Assistant integration in this server ends up making the same request:

    POST <HA_BASE_URL>/api/services/<domain>/<service>
    Authorization: Bearer <HA_API_KEY>
    Content-Type: application/json

    { "entity_id": "...", ...service data... }

:func:`call_service` is that request, and :func:`load_connection` loads the two
configuration fields it needs.

The older callers (:func:`jobs.home_assistant_blink.set_alarm` and
:func:`jobs.home_assistant_notify.notify_phone`) still build their own request,
for historical reasons only: each used to validate an extra config field of its
own, but those moved to ``home_assistant_entities.json`` (see
:mod:`jobs.home_assistant_entities`). All three loaders now require exactly
``HA_BASE_URL`` and ``HA_API_KEY``, so routing the other two through
:func:`call_service` would leave one ``requests.post`` in the codebase.

HTTP failures are reported in the return value rather than raised, matching the
rest of this server. Missing or invalid configuration still raises ValueError,
because that is a setup problem rather than a runtime one.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "configs", "home_assistant_config.json")

# The only fields every service call needs. A caller wanting more (a panel
# entity, a notify target) validates that itself.
REQUIRED_FIELDS = ("HA_BASE_URL", "HA_API_KEY")

# Home Assistant answers 200 for most service calls and 201 for some.
OK_STATUS = (200, 201)


def load_connection() -> Dict[str, str]:
    """Load the Home Assistant base URL and token.

    Raises:
        ValueError: the config file is missing, unparseable, or lacks a field.
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {CONFIG_FILE}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration file: {e}")

    for field in REQUIRED_FIELDS:
        if not config.get(field):
            raise ValueError(f"Missing required configuration field: {field}")
    return config


def call_service(
    domain: str,
    service: str,
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Call a Home Assistant service and describe the outcome.

    Args:
        domain: The service domain, e.g. "light", "switch", "scene".
        service: The service within it, e.g. "turn_on", "toggle".
        data: The service data, e.g. ``{"entity_id": "light.kitchen"}``.
        timeout: Seconds to wait for Home Assistant.

    Returns:
        dict: ``{"status": "success"|"error", "service": "<domain>.<service>", ...}``.
        A non-2xx response or a network error is reported here, not raised.

    Raises:
        ValueError: the Home Assistant configuration is missing or incomplete.
    """
    config = load_connection()
    base_url = config["HA_BASE_URL"].rstrip("/")
    endpoint = f"{base_url}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {config['HA_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = data or {}
    label = f"{domain}.{service}"

    logger.debug("Calling %s with %s", endpoint, body)

    try:
        response = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        # A Home Assistant that is down or unreachable is a runtime condition,
        # not a programming error — report it like an HTTP failure.
        error_msg = str(e)
        logger.error("%s could not reach Home Assistant: %s", label, error_msg)
        return {
            "status": "error",
            "error": f"Could not reach Home Assistant for {label}",
            "message": error_msg,
            "service": label,
        }

    logger.debug("%s -> %d %s", label, response.status_code, response.text)

    if response.status_code in OK_STATUS:
        return {
            "status": "success",
            "message": f"Called {label}",
            "service": label,
            "data": body,
        }

    error_msg = f"HTTP {response.status_code}: {response.text}"
    logger.error("%s failed: %s", label, error_msg)
    return {
        "status": "error",
        "error": f"Failed to call {label}",
        "message": error_msg,
        "service": label,
    }


def get_states() -> Optional[Dict[str, Dict[str, Any]]]:
    """Return every Home Assistant entity's state, keyed by entity id.

    Reads ``GET /api/states`` once — one round trip however many entities you
    care about, rather than one per entity. Each value looks like::

        {"state": "on", "attributes": {"brightness": 102, ...}, ...}

    Every failure — unreachable, non-200, unparseable body — is logged and
    returns ``None`` rather than raising, so a caller can report "could not read
    Home Assistant" instead of dying.

    Raises:
        ValueError: the Home Assistant configuration is missing or incomplete.
    """
    config = load_connection()
    base_url = config["HA_BASE_URL"].rstrip("/")
    endpoint = f"{base_url}/api/states"
    headers = {"Authorization": f"Bearer {config['HA_API_KEY']}"}

    try:
        response = requests.get(endpoint, headers=headers, timeout=30)
    except requests.RequestException as e:
        logger.error("could not read entity states: %s", e)
        return None

    if response.status_code != 200:
        logger.error("reading states returned HTTP %d: %s",
                     response.status_code, response.text)
        return None

    try:
        states = response.json()
    except ValueError as e:
        logger.error("unparseable state list: %s", e)
        return None

    if not isinstance(states, list):
        logger.error("expected a list of states, got %s", type(states).__name__)
        return None
    return {
        entry["entity_id"]: entry
        for entry in states
        if isinstance(entry, dict) and isinstance(entry.get("entity_id"), str)
    }


if __name__ == "__main__":
    # Manual smoke test — requires a valid home_assistant_config.json.
    print("connection ->", {k: "…" if "KEY" in k else v for k, v in load_connection().items()})
    print("call       ->", call_service("light", "turn_on", {"entity_id": "light.nonexistent"}))
    states = get_states()
    print("states     ->", f"{len(states)} entities" if states else "unavailable")
