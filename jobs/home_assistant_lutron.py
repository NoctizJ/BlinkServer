#!/usr/bin/env python3
"""Lutron light and scene control, through Home Assistant.

Two webhooks share this module (see config.json):

  - light(payload) -> POST /webhook/lutron/light
  - scene(payload) -> POST /webhook/lutron/scene

Home Assistant does not expose a Lutron-specific API. Both the ``lutron``
(RadioRA 2 / HomeWorks QS) and ``lutron_caseta`` (Caséta / RA3) integrations
register ordinary entities, so this job calls the standard services:

    light.*   ->  light.turn_on / light.turn_off / light.toggle
    switch.*  ->  switch.turn_on / switch.turn_off / switch.toggle
    scene.*   ->  scene.turn_on

The service **domain is taken from the entity id itself**, not assumed, because
Lutron's non-dimming switches arrive as ``switch.*`` rather than ``light.*``.
A ``switch`` cannot dim, so a brightness sent to one is an error rather than a
silently dropped field.

Naming
------
An entity is named either by an alias from the ``lutron`` section of
``configs/home_assistant_entities.json`` or by its full Home Assistant entity
id. Anything containing a ``.`` is treated as a raw entity id, so both of these
work and no mode flag is needed::

    {"light": "kitchen"}              # alias -> home_assistant_entities.json
    {"light": "light.kitchen_main"}   # raw entity id

Usage
-----
``light`` takes a ``state`` of ``on`` / ``off`` / ``toggle`` (defaulting to
``on``), an optional ``brightness`` as a **percentage 0-100**, and an optional
``transition`` in seconds::

    {"light": "kitchen", "state": "on", "brightness": 40}
    {"light": "kitchen", "state": "off", "transition": 2}
    {"light": "kitchen", "state": "toggle"}
    {"light": "kitchen", "brightness": 100}          # state defaults to on

``sos`` blinks an entity on and off as an attention signal, ending off::

    {"light": "kitchen"}                            # 10s, on/off every 2s, 35%
    {"light": "kitchen", "duration": 30}
    {"light": "kitchen", "duration": 20, "interval": 4}
    {"light": "kitchen", "brightness": 100}         # a brighter signal

It returns as soon as the blinking has started; the alternation runs on a
background thread. Lutron hardware needs real time to switch, so the interval is
in whole seconds rather than the milliseconds a Morse pattern would want.

``status`` reports what Home Assistant currently says about every configured
light as a short text summary — one line per state, with each on light's
brightness inline. It takes no fields.

``scene`` activates a scene, with an optional ``transition``::

    {"scene": "movie"}
    {"scene": "movie", "transition": 3}

Home Assistant scenes have no "off" and no toggle — activating is all a scene
can do, so a second scene is how you undo the first.

Switches
--------
Gated by two switches, like the other Home Assistant jobs:

  * the ``lutron`` feature in ``configs/home_assistant_switches.json``, checked
    here, which turns this integration off entirely;
  * the ``lutron`` job in ``configs/job_switches.json``, checked by the webhook
    dispatcher, which stops both paths responding at all.

Nothing here raises: a bad payload or a failing call is reported in the return
value, so a webhook gets a JSON error instead of a 500.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.home_assistant_api import call_service, get_states
    from jobs.home_assistant_entities import aliases as ha_aliases
    from jobs.home_assistant_switches import LUTRON as HA_LUTRON_FEATURE
    from jobs.home_assistant_switches import enabled_for as ha_feature_enabled
    from jobs.home_assistant_switches import skipped as ha_feature_skipped
    from jobs.log_engine import log as write_log
    from jobs.text_format import rule
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_api import call_service, get_states
    from home_assistant_entities import aliases as ha_aliases
    from home_assistant_switches import LUTRON as HA_LUTRON_FEATURE
    from home_assistant_switches import enabled_for as ha_feature_enabled
    from home_assistant_switches import skipped as ha_feature_skipped
    from log_engine import log as write_log
    from text_format import rule

logger = logging.getLogger(__name__)

# Where the alias -> entity id maps live in home_assistant_entities.json.
ENTITIES_FEATURE = "lutron"
LIGHTS_SECTION = "lights"
SCENES_SECTION = "scenes"

# Payload key naming the thing to control, per endpoint.
LIGHT_KEYS = ("light",)
SCENE_KEYS = ("scene",)

# Entity domains each endpoint accepts. Lutron dimmers are lights; its non-dim
# switches are switches; both turn on, off, and toggle the same way.
LIGHT_DOMAINS = ("light", "switch")
SCENE_DOMAINS = ("scene",)

# Domains that can be dimmed. A brightness for anything else is rejected.
DIMMABLE_DOMAINS = ("light",)

# Accepted spellings for the light state, mapped to the Home Assistant service.
STATE_SERVICES = {
    "on": "turn_on",
    "true": "turn_on",
    "off": "turn_off",
    "false": "turn_off",
    "toggle": "toggle",
}

# The state used when a light request does not say.
DEFAULT_STATE = "on"

# SOS blink timing, in SECONDS. Lutron hardware needs real time to switch — a
# sub-second blink is either invisible or arrives as a dim flicker — so the
# pattern is a plain alternation rather than Morse.
SOS_DEFAULT_DURATION = 10.0
SOS_MIN_DURATION = 2.0
SOS_MAX_DURATION = 300.0

# The level each on-step drives a dimmer to. Without an explicit brightness the
# integration picks the level itself — some restore the last one used, so a light
# last left at 5% would blink almost invisibly. Sending it makes the signal
# deterministic.
SOS_DEFAULT_BRIGHTNESS = 35.0

SOS_DEFAULT_INTERVAL = 2.0
SOS_MIN_INTERVAL = 1.0
SOS_MAX_INTERVAL = 60.0

# Dimmers fade by default, which softens the edge of a blink, so each step asks
# for an instant change. Only lights take a transition — sending one to a switch
# is a schema error.
INSTANT = {"transition": 0}

# Entities with an SOS running, so a second request cannot fight the first.
_running: set = set()
_running_lock = threading.Lock()


def _error(error: str, message: str) -> Dict[str, Any]:
    """A webhook error result, reported rather than raised."""
    return {"status": "error", "error": error, "message": message}


def all_aliases(section: str) -> Dict[str, str]:
    """Return one section's alias -> entity id map, or ``{}``.

    Comes from the ``lutron`` section of home_assistant_entities.json. A missing
    or unreadable file is not fatal — raw entity ids still work, so the job
    degrades to "no aliases" rather than failing.
    """
    return ha_aliases(ENTITIES_FEATURE, section)


def _normalize_state(value: Any) -> str:
    """Map a caller's ``state`` spelling to a lookup key for STATE_SERVICES.

    A missing or blank state means :data:`DEFAULT_STATE`. Booleans are stringified
    first — the same thing :func:`jobs.presence_webhook._normalize_state` does —
    because a bare ``or`` treats JSON ``false`` as absent and would turn a light
    *on* when asked to turn it off.
    """
    if isinstance(value, bool):
        value = str(value)
    if value is None or not str(value).strip():
        value = DEFAULT_STATE
    return str(value).strip().lower()


def _name_from(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    """Return the first of ``keys`` the payload actually sets, stripped."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def resolve_entity(name: str, section: str) -> Optional[str]:
    """Turn an alias or raw entity id into an entity id, or ``None``.

    A ``name`` containing a ``.`` is taken as an entity id as-is; anything else
    is looked up in ``section`` of the entities file's ``lutron`` section.
    """
    if "." in name:
        return name
    return all_aliases(section).get(name)


def _brightness_pct(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Read and validate ``brightness`` as a percentage, or return an error text."""
    if "brightness" not in payload:
        return None, None
    value = payload["brightness"]
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None, f"brightness {value!r} is not a number (expected 0-100)"
    if not 0 <= percent <= 100:
        return None, f"brightness {percent:g} is out of range (expected 0-100)"
    return percent, None


def _transition(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Read and validate ``transition`` in seconds, or return an error text."""
    if "transition" not in payload:
        return None, None
    value = payload["transition"]
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None, f"transition {value!r} is not a number (expected seconds)"
    if seconds < 0:
        return None, f"transition {seconds:g} cannot be negative"
    return seconds, None


def _bounded_number(
    payload: Dict[str, Any],
    key: str,
    default: float,
    low: float,
    high: float,
) -> Tuple[float, Optional[str]]:
    """Read an optional numeric field within bounds, or return an error text."""
    if key not in payload:
        return default, None
    value = payload[key]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default, f"{key} {value!r} is not a number"
    if not low <= number <= high:
        return default, f"{key} {number:g} is out of range (expected {low:g}-{high:g})"
    return number, None


def _run(domain: str, service: str, data: Dict[str, Any], what: str) -> Dict[str, Any]:
    """Make the call, logging the outcome under the "blink" log type."""
    try:
        result = call_service(domain, service, data)
    except Exception as e:  # missing/invalid home_assistant_config.json
        error_msg = str(e)
        logger.error("lutron %s failed: %s", what, error_msg)
        write_log("blink", f"LUTRON {what} ERROR: {error_msg}")
        return _error("Lutron call failed", error_msg)

    write_log(
        "blink",
        f"LUTRON {what}: {domain}.{service} {data} -> {result.get('status')}",
    )
    return result


def light(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/lutron/light.

    Turns a Lutron light or switch on, off, or toggles it, optionally setting a
    brightness percentage and a transition time.
    """
    logger.debug("lutron light payload: %s", payload)

    if not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")

    if not ha_feature_enabled(HA_LUTRON_FEATURE):
        return ha_feature_skipped(HA_LUTRON_FEATURE)

    name = _name_from(payload, LIGHT_KEYS)
    if not name:
        return _error(
            "Missing light",
            f"Payload must name a light in one of: {', '.join(LIGHT_KEYS)}",
        )

    entity_id = resolve_entity(name, LIGHTS_SECTION)
    if not entity_id:
        known = ", ".join(sorted(all_aliases(LIGHTS_SECTION))) or "(none configured)"
        return _error(
            "Unknown light",
            f"{name!r} is not a known alias and is not an entity id. "
            f"Known aliases: {known}",
        )

    domain = entity_id.split(".", 1)[0]
    if domain not in LIGHT_DOMAINS:
        return _error(
            "Wrong entity domain",
            f"{entity_id!r} is a '{domain}' entity - this endpoint controls "
            f"{' or '.join(LIGHT_DOMAINS)} entities (use /webhook/lutron/scene for scenes)",
        )

    state = _normalize_state(payload.get("state"))
    service = STATE_SERVICES.get(state)
    if not service:
        return _error(
            "Invalid state",
            f"Invalid state {payload.get('state')!r} - must be one of: "
            f"{', '.join(sorted(STATE_SERVICES))}",
        )

    percent, error_msg = _brightness_pct(payload)
    if error_msg:
        return _error("Invalid brightness", error_msg)

    seconds, error_msg = _transition(payload)
    if error_msg:
        return _error("Invalid transition", error_msg)

    data: Dict[str, Any] = {"entity_id": entity_id}

    if percent is not None:
        if domain not in DIMMABLE_DOMAINS:
            return _error(
                "Not dimmable",
                f"{entity_id!r} is a '{domain}' entity and cannot be dimmed - "
                f"drop 'brightness', or point this alias at a light entity",
            )
        if service == "turn_off":
            return _error(
                "Conflicting request",
                "'brightness' cannot be combined with state 'off' - "
                "use brightness 0 to turn it off by dimming",
            )
        data["brightness_pct"] = percent

    if seconds is not None:
        data["transition"] = seconds

    return _run(domain, service, data, f"light {name}")


def scene(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/lutron/scene.

    Activates a Lutron scene. Home Assistant scenes cannot be turned off or
    toggled, so activating is the only thing this does — undo a scene by
    activating another one.
    """
    logger.debug("lutron scene payload: %s", payload)

    if not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")

    if not ha_feature_enabled(HA_LUTRON_FEATURE):
        return ha_feature_skipped(HA_LUTRON_FEATURE)

    name = _name_from(payload, SCENE_KEYS)
    if not name:
        return _error(
            "Missing scene",
            f"Payload must name a scene in one of: {', '.join(SCENE_KEYS)}",
        )

    entity_id = resolve_entity(name, SCENES_SECTION)
    if not entity_id:
        known = ", ".join(sorted(all_aliases(SCENES_SECTION))) or "(none configured)"
        return _error(
            "Unknown scene",
            f"{name!r} is not a known alias and is not an entity id. "
            f"Known aliases: {known}",
        )

    domain = entity_id.split(".", 1)[0]
    if domain not in SCENE_DOMAINS:
        return _error(
            "Wrong entity domain",
            f"{entity_id!r} is a '{domain}' entity - this endpoint activates "
            f"scene entities (use /webhook/lutron/light for lights)",
        )

    seconds, error_msg = _transition(payload)
    if error_msg:
        return _error("Invalid transition", error_msg)

    data: Dict[str, Any] = {"entity_id": entity_id}
    if seconds is not None:
        data["transition"] = seconds

    return _run(domain, "turn_on", data, f"scene {name}")



def _blink_steps(duration: float, interval: float) -> List[Tuple[bool, float]]:
    """Return the blink as ``(on, seconds)`` steps, always ending off.

    The entity alternates every ``interval`` seconds for ``duration`` seconds. If
    the last period was an on, a final off with no wait is appended — so the
    light is dark when this finishes, whatever it was before.

        duration 10, interval 2  ->  on off on off on  + off
                                     (3 on, 3 off, 6 calls, 10s)
    """
    count = max(1, int(duration // interval))
    steps: List[Tuple[bool, float]] = [(index % 2 == 0, interval) for index in range(count)]
    if steps[-1][0]:
        steps.append((False, 0.0))
    return steps


def _run_sos(
    entity_id: str,
    domain: str,
    duration: float,
    interval: float,
    brightness: float,
) -> None:
    """Blink an entity on and off, ending off. Runs on a background thread.

    Each step subtracts the time its own service call took from the wait, so a
    slow Home Assistant does not stretch the blink. Nothing raises out of here —
    a thread that died silently would leave the entity latched on and its name
    stuck in ``_running``.
    """
    steps = _blink_steps(duration, interval)
    failures = 0
    try:
        for on, seconds in steps:
            started = time.monotonic()
            data: Dict[str, Any] = {"entity_id": entity_id}
            if domain in DIMMABLE_DOMAINS:
                data.update(INSTANT)
                if on:
                    data["brightness_pct"] = brightness
            result = call_service(domain, "turn_on" if on else "turn_off", data)
            if result.get("status") != "success":
                failures += 1
            elapsed = time.monotonic() - started
            time.sleep(max(0, seconds - elapsed))
        write_log(
            "blink",
            f"LUTRON SOS {entity_id}: {duration:g}s at {interval:g}s, "
            f"{len(steps)} calls, {failures} failed, ended off",
        )
    except Exception as e:  # a dying thread must not leave the entity latched on
        logger.exception("SOS on %s failed", entity_id)
        write_log("blink", f"LUTRON SOS {entity_id} ERROR: {e}")
    finally:
        with _running_lock:
            _running.discard(entity_id)


def sos(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/lutron/sos.

    Blinks a Lutron light or switch on and off as an attention signal, ending
    off. Returns as soon as the blinking has been started — it runs on a
    background thread, because holding the request open for ten seconds or more
    would time out a Shortcut.

    Fields: ``light`` (alias or entity id), ``duration`` in seconds (default 10,
    2-300), ``interval`` in seconds (default 2, 1-60), and ``brightness`` as a
    percentage (default 35, 1-100; lights only — a switch has no level).
    """
    logger.debug("lutron sos payload: %s", payload)

    if not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")

    if not ha_feature_enabled(HA_LUTRON_FEATURE):
        return ha_feature_skipped(HA_LUTRON_FEATURE)

    name = _name_from(payload, LIGHT_KEYS)
    if not name:
        return _error(
            "Missing light",
            f"Payload must name a light in one of: {', '.join(LIGHT_KEYS)}",
        )

    entity_id = resolve_entity(name, LIGHTS_SECTION)
    if not entity_id:
        known = ", ".join(sorted(all_aliases(LIGHTS_SECTION))) or "(none configured)"
        return _error(
            "Unknown light",
            f"{name!r} is not a known alias and is not an entity id. "
            f"Known aliases: {known}",
        )

    domain = entity_id.split(".", 1)[0]
    if domain not in LIGHT_DOMAINS:
        return _error(
            "Wrong entity domain",
            f"{entity_id!r} is a '{domain}' entity - SOS blinks "
            f"{' or '.join(LIGHT_DOMAINS)} entities",
        )

    duration, error_msg = _bounded_number(
        payload, "duration", SOS_DEFAULT_DURATION, SOS_MIN_DURATION, SOS_MAX_DURATION)
    if error_msg:
        return _error("Invalid duration", error_msg)

    interval, error_msg = _bounded_number(
        payload, "interval", SOS_DEFAULT_INTERVAL, SOS_MIN_INTERVAL, SOS_MAX_INTERVAL)
    if error_msg:
        return _error("Invalid interval", error_msg)

    if interval > duration:
        return _error(
            "Invalid interval",
            f"interval {interval:g}s is longer than the duration {duration:g}s",
        )

    percent, error_msg = _brightness_pct(payload)
    if error_msg:
        return _error("Invalid brightness", error_msg)
    if percent is not None and domain not in DIMMABLE_DOMAINS:
        # Defaulted brightness is fine to ignore on a switch; an explicit one is a
        # request we cannot honour, so say so rather than drop it.
        return _error(
            "Not dimmable",
            f"{entity_id!r} is a '{domain}' entity and cannot be dimmed - "
            f"drop 'brightness'",
        )
    if percent == 0:
        return _error(
            "Invalid brightness",
            "brightness 0 would blink the light at nothing - use 1-100",
        )
    brightness = SOS_DEFAULT_BRIGHTNESS if percent is None else percent

    # One SOS at a time per entity, or two threads fight over the same light.
    with _running_lock:
        if entity_id in _running:
            return _error(
                "Already running",
                f"an SOS is already blinking {entity_id} - wait for it to finish",
            )
        _running.add(entity_id)

    steps = _blink_steps(duration, interval)
    try:
        thread = threading.Thread(
            target=_run_sos,
            args=(entity_id, domain, duration, interval, brightness),
            name=f"sos-{entity_id}",
            daemon=True,
        )
        thread.start()
    except Exception as e:
        with _running_lock:
            _running.discard(entity_id)
        return _error("Could not start SOS", str(e))

    return {
        "status": "started",
        "entity_id": entity_id,
        "duration": duration,
        "interval": interval,
        "brightness": brightness if domain in DIMMABLE_DOMAINS else None,
        "calls": len(steps),
        "estimated_seconds": round(sum(seconds for _, seconds in steps), 1),
        "message": f"Blinking {entity_id} for {duration:g}s every {interval:g}s, ending off",
    }



# States Home Assistant reports that mean "I could not reach this device".
UNREACHABLE_STATES = ("unavailable", "unknown")

# Shown in the brightness column for anything without a readable level.
NO_BRIGHTNESS = "-"


def _brightness_of(entry: Dict[str, Any]) -> str:
    """Render an entity's brightness as a percentage, or ``"-"``.

    Home Assistant reports ``brightness`` as 0-255; switches have none at all.
    """
    brightness = (entry.get("attributes") or {}).get("brightness")
    if not isinstance(brightness, (int, float)):
        return NO_BRIGHTNESS
    return f"{round(brightness / 255 * 100)}%"


def _light_rows(states: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair every configured light alias with what Home Assistant says about it.

    An alias pointing at an entity Home Assistant does not know is reported as
    ``"missing"`` rather than dropped — a stale alias is the thing you most want
    this report to show you.
    """
    rows = []
    for alias, entity_id in sorted(all_aliases(LIGHTS_SECTION).items()):
        entry = states.get(entity_id)
        if entry is None:
            rows.append({
                "alias": alias,
                "entity_id": entity_id,
                "state": "missing",
                "brightness": NO_BRIGHTNESS,
                "name": "",
            })
            continue
        rows.append({
            "alias": alias,
            "entity_id": entity_id,
            "state": str(entry.get("state") or "unknown"),
            "brightness": _brightness_of(entry),
            "name": str((entry.get("attributes") or {}).get("friendly_name") or ""),
        })
    return rows


def format_lights(rows: List[Dict[str, Any]]) -> str:
    """Render the light rows as a short plain text report.

    A count, a rule, and one line per state — with each on light's brightness
    inline, so the whole picture fits in three lines::

        Lutron lights — 4 lights
        ----------------------------------------
        On (2): dining (10%), living (40%)
        Off (1): kitchen
        Missing/unavailable (1): porch

    The per-light entity ids and friendly names are in the ``lights`` field of the
    result rather than here. The ``Missing/unavailable`` line only appears when
    something is actually wrong.
    """
    if not rows:
        return "Lutron lights — none configured."

    def labelled(row: Dict[str, Any]) -> str:
        """A light's alias, with its brightness when it has one."""
        if row["brightness"] == NO_BRIGHTNESS:
            return row["alias"]
        return f"{row['alias']} ({row['brightness']})"

    on = sorted(labelled(r) for r in rows if r["state"] == "on")
    off = sorted(r["alias"] for r in rows if r["state"] == "off")
    other = sorted(r["alias"] for r in rows if r["state"] not in ("on", "off"))

    count = len(rows)
    header = f"Lutron lights — {count} {'light' if count == 1 else 'lights'}"
    summary = [f"On ({len(on)}): {', '.join(on) or NO_BRIGHTNESS}",
               f"Off ({len(off)}): {', '.join(off) or NO_BRIGHTNESS}"]
    if other:
        summary.append(f"Missing/unavailable ({len(other)}): {', '.join(other)}")

    return "\n".join([header, rule([header] + summary)] + summary)


def status(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/lutron/status.

    Reports what Home Assistant currently says about every light aliased in the
    ``lutron`` section of home_assistant_entities.json — its state, its
    brightness, and its friendly name — as both structured fields and a
    ready-to-display ``message``, the way the presence read does.

    Every configured alias is read in a single ``GET /api/states``, so the cost
    does not grow with the number of lights. An alias whose entity Home Assistant
    does not know is reported as ``"missing"`` rather than omitted.
    """
    logger.debug("lutron status payload: %s", payload)

    if not ha_feature_enabled(HA_LUTRON_FEATURE):
        return ha_feature_skipped(HA_LUTRON_FEATURE)

    aliases = all_aliases(LIGHTS_SECTION)
    if not aliases:
        return {
            "status": "ok",
            "count": 0,
            "on": [],
            "off": [],
            "other": [],
            "lights": {},
            "message": format_lights([]),
        }

    try:
        states = get_states()
    except Exception as e:  # missing/invalid home_assistant_config.json
        error_msg = str(e)
        logger.error("lutron status failed: %s", error_msg)
        return _error("Lutron status failed", error_msg)

    if states is None:
        return _error(
            "Home Assistant unreachable",
            "could not read entity states from Home Assistant",
        )

    rows = _light_rows(states)
    return {
        "status": "ok",
        "count": len(rows),
        "on": sorted(r["alias"] for r in rows if r["state"] == "on"),
        "off": sorted(r["alias"] for r in rows if r["state"] == "off"),
        "other": sorted(r["alias"] for r in rows if r["state"] not in ("on", "off")),
        "lights": {
            r["alias"]: {
                "entity_id": r["entity_id"],
                "state": r["state"],
                "brightness": r["brightness"],
                "name": r["name"],
            }
            for r in rows
        },
        "message": format_lights(rows),
    }


if __name__ == "__main__":
    # Simple smoke test / demo — reaches Home Assistant only if configured.
    print("light aliases ->", all_aliases(LIGHTS_SECTION))
    print("scene aliases ->", all_aliases(SCENES_SECTION))
    print("no name       ->", light({}))
    print("unknown       ->", light({"light": "nope"}))
    print("bad state     ->", light({"light": "light.x", "state": "sideways"}))
    print("bad brightness->", light({"light": "light.x", "brightness": 140}))
    print("not dimmable  ->", light({"light": "switch.x", "brightness": 40}))
    print("wrong domain  ->", light({"light": "scene.x"}))
    print("scene no name ->", scene({}))
    print("sos no name   ->", sos({}))
    print("sos bad dur   ->", sos({"light": "light.x", "duration": 9999}))
    print("sos steps     ->", _blink_steps(SOS_DEFAULT_DURATION, SOS_DEFAULT_INTERVAL))
    print("\nstatus report:\n")
    print(status({}).get("message"))
