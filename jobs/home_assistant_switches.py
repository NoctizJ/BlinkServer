#!/usr/bin/env python3
"""Feature switches for this server's Home Assistant integration.

This is the top tier of the switch hierarchy — it decides whether we talk to
Home Assistant *at all*, independently of which job or which notification asked:

    home_assistant_switches.json   does this server use Home Assistant?
      ├── blink                    ... to arm/disarm the Blink alarm panel
      ├── notify                   ... to push notifications to a phone
      ├── lutron                   ... to control Lutron lights and scenes
      └── speak                    ... to say something on a media player

    job_switches.json              which webhook jobs are live
    log_switches.json              which log types are written
    notify_switches.json           which notifications are sent

Each feature is checked at the single point where that Home Assistant API is
called, so one switch covers every caller:

  * ``blink`` — :func:`jobs.home_assistant_blink.set_alarm`, used by
    ``/webhook/blink/*`` **and** the leaving/arriving webhooks;
  * ``notify`` — :func:`jobs.home_assistant_notify.notify_phone`, used by the
    leaving/arriving, location, and arm/disarm notifications;
  * ``lutron`` — the :mod:`jobs.home_assistant_lutron` handlers, used by
    ``/webhook/lutron/*``;
  * ``speak`` — :mod:`jobs.home_assistant_speak`, used by ``/webhook/speak``.

Turning a feature off makes the call a no-op that reports ``"skipped"`` — no HTTP
request is made, and ``home_assistant_config.json`` is not even read, so a
feature can be switched off before it is configured. Nothing raises.

The file looks like::

    {
      "features": { "blink": true, "notify": true, "lutron": true, "speak": true },
      "last_modified": "2026-08-30 12:04:11.221"
    }

Toggle over HTTP with ``POST /ha/<feature>/enable|disable|toggle``, and list with
``GET /ha``. An unknown feature counts as on and is registered on first check,
matching every other switch file.
"""

import logging
from typing import Dict

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.switches import REPO_ROOT, all_switches, is_enabled, set_enabled
except ImportError:  # pragma: no cover - allows running this file directly
    from switches import REPO_ROOT, all_switches, is_enabled, set_enabled

logger = logging.getLogger(__name__)

SWITCH_FILE = REPO_ROOT / "configs" / "home_assistant_switches.json"
SWITCH_SECTION = "features"

# Arming/disarming the Blink alarm panel through Home Assistant.
BLINK = "blink"

# Pushing notifications to a phone through Home Assistant.
NOTIFY = "notify"

# Controlling Lutron lights and scenes through Home Assistant.
LUTRON = "lutron"

# Speaking a message on a media player through Home Assistant.
SPEAK = "speak"

# Every feature this file governs, for validating a toggle request.
FEATURES = (BLINK, NOTIFY, LUTRON, SPEAK)


def enabled_for(feature: str) -> bool:
    """Return whether a Home Assistant feature is on, registering new ones."""
    return is_enabled(SWITCH_FILE, SWITCH_SECTION, feature)


def set_enabled_for(feature: str, enabled: bool) -> bool:
    """Turn a Home Assistant feature on or off; returns the value written."""
    logger.info("Home Assistant %s %s", feature, "enabled" if enabled else "disabled")
    return set_enabled(SWITCH_FILE, SWITCH_SECTION, feature, enabled)


def all_enabled() -> Dict[str, bool]:
    """Return every Home Assistant feature switch, keyed by feature."""
    return all_switches(SWITCH_FILE, SWITCH_SECTION)


def skipped(feature: str) -> Dict[str, str]:
    """The result a Home Assistant call returns when its feature is off."""
    return {
        "status": "skipped",
        "message": f"Home Assistant '{feature}' is off (home_assistant_switches.json)",
    }


if __name__ == "__main__":
    # Simple smoke test / demo — toggles the real switch file.
    print("all        ->", all_enabled())
    print("blink on?  ->", enabled_for(BLINK))
    print("notify on? ->", enabled_for(NOTIFY))
    print("lutron on? ->", enabled_for(LUTRON))
    print("speak on?  ->", enabled_for(SPEAK))
    print("disable    ->", set_enabled_for(BLINK, False))
    print("skipped    ->", skipped(BLINK))
    print("re-enable  ->", set_enabled_for(BLINK, True))
    print("file       ->", SWITCH_FILE)
