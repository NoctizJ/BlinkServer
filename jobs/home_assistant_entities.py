#!/usr/bin/env python3
"""Home Assistant entity references for this server.

Which entities we talk to, kept apart from how we connect to Home Assistant:

    home_assistant_config.json     WHERE Home Assistant is, and the token
                                   (gitignored — it holds a secret)
    home_assistant_entities.json   WHICH entities we act on
                                   (tracked — just names, no secrets)
    home_assistant_switches.json   WHETHER each feature is used at all

An entity id is a name, not a credential, so keeping these in the tracked file
means the mapping survives a fresh clone and shows up in diffs. The sections are
named after the Home Assistant features in
:mod:`jobs.home_assistant_switches`, so the two files line up::

    {
      "blink":  { "panel_AMS": "alarm_control_panel.blink_armstrong" },
      "notify": { "target":    "mobile_app_aisingioro" },
      "lutron": {
        "lights": { "kitchen": "light.kitchen_main" },
        "scenes": { "movie":   "scene.movie_night" }
      }
    }

Per-home entities
-----------------
Keys that differ per house carry the home name, using the same names as
``state/presence.json`` — so the default home ``AMS`` has ``panel_AMS``, and a
second house ``M`` would add ``panel_M`` beside it.

Usage:
    from jobs.home_assistant_entities import entity, aliases

    entity("blink", "panel_AMS")
    aliases("lutron", "lights")            # -> {"kitchen": "light.kitchen_main"}
"""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "configs", "home_assistant_entities.json"
)


def _load(path: str, what: str) -> Dict[str, Any]:
    """Load a JSON object from ``path``, or ``{}``.

    A missing file is normal — the entities file may not exist yet. Unreadable
    JSON is logged and treated the same way, so a typo cannot take an endpoint
    down without explanation.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", what, e)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_entities() -> Dict[str, Any]:
    """Return home_assistant_entities.json, or ``{}``."""
    return _load(CONFIG_FILE, "home_assistant_entities.json")


def section(feature: str) -> Dict[str, Any]:
    """Return one feature's section from the entities file, or ``{}``."""
    entry = load_entities().get(feature)
    return entry if isinstance(entry, dict) else {}


def entity(feature: str, key: str) -> Optional[str]:
    """Return one entity reference, or ``None`` if it is not configured.

    Args:
        feature: The section, e.g. ``"blink"`` or ``"notify"``.
        key: The name within it, e.g. ``"panel_AMS"`` or ``"target"``.
    """
    value = section(feature).get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def aliases(feature: str, key: str) -> Dict[str, str]:
    """Return an alias map from a feature's section, e.g. lutron -> lights."""
    entry = section(feature).get(key)
    return entry if isinstance(entry, dict) else {}


if __name__ == "__main__":
    # Simple smoke test / demo — reads the real config files.
    print("entities      ->", load_entities())
    print("blink panel   ->", entity("blink", "panel_AMS"))
    print("notify target ->", entity("notify", "target"))
    print("lutron lights ->", aliases("lutron", "lights"))
    print("lutron scenes ->", aliases("lutron", "scenes"))
    print("missing       ->", entity("blink", "nonexistent"))
