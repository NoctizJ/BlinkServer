#!/usr/bin/env python3
"""Text-to-speech through Home Assistant — make a speaker say something.

One webhook (see config.json):

  - speak(payload) -> POST /webhook/speak/ha

The ``/ha`` suffix distinguishes this from :mod:`jobs.airplay_speak`, which says
the same thing straight to an AirPlay speaker without Home Assistant in the way.

Home Assistant's ``tts.speak`` service takes the **TTS provider** as its target
entity and the speaker separately, which is easy to get backwards::

    POST /api/services/tts/speak
    {
      "entity_id": "tts.google_en_com",              <- the provider
      "media_player_entity_id": "media_player.homepod_mini",   <- the speaker
      "message": "The alarm has been armed."
    }

Both entity ids live in the ``speak`` section of
``configs/home_assistant_entities.json``::

    "speak": {
      "tts": "tts.google_en_com",
      "speakers": {
        "homepod": "media_player.homepod_mini",
        "kitchen": "media_player.kitchen_homepod"
      }
    }

Every request is logged to ``logs/default.log`` with the requester's ``id``, with
the spoken words on their own line::

    [2026-08-30 15:02:11.482] [DEFAULT]
    ----------------------------------------------------------------
    SPEAK by 娜 on media_player.homepod_mini at 80% -> success
    "Dinner is ready"

A request names a speaker by alias or by full entity id, the same way the Lutron
handlers do — anything containing a ``.`` is taken as an entity id::

    {"message": "Dinner is ready"}                        # the only speaker
    {"message": "Dinner is ready", "id": "Alex"}          # who asked
    {"message": "Dinner is ready", "speaker": "kitchen"}
    {"message": "おかえりなさい", "language": "ja"}
    {"message": "Hello", "speaker": "media_player.unlisted"}

``speaker`` may be omitted when exactly one is configured; with several it must be
named, and the error lists them.

Switches
--------
Gated by two switches, like the other Home Assistant jobs:

  * the ``speak`` feature in ``configs/home_assistant_switches.json``, checked
    here, which turns text-to-speech off entirely. **It ships off**, because
    Home Assistant cannot currently discover the HomePod; enable it with
    ``POST /ha/speak/enable`` once it can;
  * the ``home_assistant_speak`` job in ``configs/job_switches.json``, checked by
    the webhook dispatcher, which stops the path responding at all.

Nothing here raises: a bad payload or a failing call is reported in the return
value, so a webhook gets a JSON error instead of a 500.
"""

import logging
from typing import Any, Dict, Optional

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.home_assistant_api import call_service
    from jobs.home_assistant_entities import aliases as ha_aliases
    from jobs.home_assistant_entities import entity as ha_entity
    from jobs.home_assistant_switches import SPEAK as HA_SPEAK_FEATURE
    from jobs.home_assistant_switches import enabled_for as ha_feature_enabled
    from jobs.home_assistant_switches import skipped as ha_feature_skipped
    from jobs.log_engine import DEFAULT_TYPE as LOG_TYPE
    from jobs.log_engine import log as write_log
    from jobs.presence_state import resolve_person
except ImportError:  # pragma: no cover - allows running this file directly
    from home_assistant_api import call_service
    from home_assistant_entities import aliases as ha_aliases
    from home_assistant_entities import entity as ha_entity
    from home_assistant_switches import SPEAK as HA_SPEAK_FEATURE
    from home_assistant_switches import enabled_for as ha_feature_enabled
    from home_assistant_switches import skipped as ha_feature_skipped
    from log_engine import DEFAULT_TYPE as LOG_TYPE
    from log_engine import log as write_log
    from presence_state import resolve_person

logger = logging.getLogger(__name__)

# Where the TTS provider and the speaker aliases live in the entities file.
ENTITIES_FEATURE = "speak"
TTS_KEY = "tts"
SPEAKERS_SECTION = "speakers"

# The Home Assistant service, and the domains each of its two entities must be.
TTS_DOMAIN = "tts"
TTS_SERVICE = "speak"
SPEAKER_DOMAIN = "media_player"

# Setting the volume is a separate service — tts.speak has no volume of its own.
VOLUME_SERVICE = "volume_set"

# Payload keys.
MESSAGE_KEYS = ("message", "text")
SPEAKER_KEYS = ("speaker", "media_player")

# Who asked. Resolved like every other "id" in this server, so a post without one
# is attributed to jobs.presence_state.DEFAULT_PERSON rather than to nobody.
ID_KEY = "id"

# Long enough for any announcement, short enough that a runaway caller cannot
# queue minutes of speech on a speaker nobody can stop from here.
MAX_MESSAGE_LENGTH = 500


def _error(error: str, message: str) -> Dict[str, Any]:
    """A webhook error result, reported rather than raised."""
    return {"status": "error", "error": error, "message": message}


def _percentage(payload: Dict[str, Any], key: str) -> Any:
    """Read an optional 1-100 percentage, returning ``(value, error_text)``.

    Zero is rejected rather than accepted: a volume of 0 would speak silently
    while reporting success. (:mod:`jobs.home_assistant_lutron` has its own
    bounded-number reader for the same reason — a shared module for one small
    function would cost more than it saves.)
    """
    if key not in payload:
        return None, None
    value = payload[key]
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None, f"{key} {value!r} is not a number (expected 1-100)"
    if percent == 0:
        return None, f"{key} 0 would speak silently - use 1-100"
    if not 1 <= percent <= 100:
        return None, f"{key} {percent:g} is out of range (expected 1-100)"
    return percent, None


def all_speakers() -> Dict[str, str]:
    """Return the alias -> media_player entity id map, or ``{}``."""
    return ha_aliases(ENTITIES_FEATURE, SPEAKERS_SECTION)


def tts_entity() -> Optional[str]:
    """Return the configured TTS provider entity id, or ``None``."""
    return ha_entity(ENTITIES_FEATURE, TTS_KEY)


def _first(payload: Dict[str, Any], keys: tuple) -> Optional[str]:
    """Return the first of ``keys`` the payload actually sets, stripped."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def resolve_speaker(name: Optional[str]) -> Optional[str]:
    """Turn a speaker alias or entity id into an entity id, or ``None``.

    A ``name`` containing a ``.`` is taken as an entity id as-is. With no name at
    all, the single configured speaker is used — which is only unambiguous when
    exactly one is configured.
    """
    speakers = all_speakers()
    if name is None:
        return next(iter(speakers.values())) if len(speakers) == 1 else None
    if "." in name:
        return name
    return speakers.get(name)


def speak(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/speak/ha.

    Says ``message`` on a Home Assistant media player through the configured TTS
    provider. Fields: ``message`` (required), ``id`` (who asked; defaults to the
    presence store's default person), ``speaker`` (alias or entity id, optional
    when only one is configured), ``language`` (optional, passed through to the
    provider — Google Translate TTS takes e.g. "en", "ja"), and ``volume`` as a
    percentage 1-100 (optional).

    Every request is written to the **default** log with the requester, the
    speaker, and what was said — this is the one job that puts words in the
    house's mouth, so who asked for it is worth keeping.

    A ``volume`` is applied with ``media_player.volume_set`` before speaking, and
    **stays** afterwards. Restoring the previous level is not possible from here:
    ``tts.speak`` returns once Home Assistant has queued the audio, not when the
    speech ends, so there is no moment to restore at without guessing how long the
    message takes to say.
    """
    logger.debug("speak payload: %s", payload)

    if not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")

    if not ha_feature_enabled(HA_SPEAK_FEATURE):
        return ha_feature_skipped(HA_SPEAK_FEATURE)

    person = resolve_person(payload)

    message = _first(payload, MESSAGE_KEYS)
    if not message:
        return _error(
            "Missing message",
            f"Payload must include a non-empty message in one of: "
            f"{', '.join(MESSAGE_KEYS)}",
        )
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(
            "Message too long",
            f"message is {len(message)} characters, the limit is {MAX_MESSAGE_LENGTH}",
        )

    volume, error_msg = _percentage(payload, "volume")
    if error_msg:
        return _error("Invalid volume", error_msg)

    provider = tts_entity()
    if not provider:
        return _error(
            "No TTS provider configured",
            f"set '{TTS_KEY}' under '{ENTITIES_FEATURE}' in "
            f"configs/home_assistant_entities.json (e.g. 'tts.google_en_com')",
        )
    if not provider.startswith(f"{TTS_DOMAIN}."):
        return _error(
            "Wrong TTS entity",
            f"{provider!r} is not a '{TTS_DOMAIN}' entity - the provider is the "
            f"tts.* entity, not the speaker",
        )

    name = _first(payload, SPEAKER_KEYS)
    speaker = resolve_speaker(name)
    if not speaker:
        known = ", ".join(sorted(all_speakers())) or "(none configured)"
        if name is None:
            return _error(
                "Missing speaker",
                f"Payload must name a speaker in one of: {', '.join(SPEAKER_KEYS)} "
                f"(only optional when exactly one is configured). "
                f"Known speakers: {known}",
            )
        return _error(
            "Unknown speaker",
            f"{name!r} is not a known alias and is not an entity id. "
            f"Known speakers: {known}",
        )
    if not speaker.startswith(f"{SPEAKER_DOMAIN}."):
        return _error(
            "Wrong speaker domain",
            f"{speaker!r} is a '{speaker.split('.', 1)[0]}' entity - a speaker is a "
            f"{SPEAKER_DOMAIN}.* entity",
        )

    data: Dict[str, Any] = {
        "entity_id": provider,
        "media_player_entity_id": speaker,
        "message": message,
    }
    language = _first(payload, ("language",))
    if language:
        data["language"] = language

    volume_error = None
    try:
        if volume is not None:
            # Home Assistant takes a 0.0-1.0 level, not a percentage. Speaking at
            # the wrong volume beats not speaking, so a failure here is recorded
            # and the message still goes out.
            volume_result = call_service(
                SPEAKER_DOMAIN, VOLUME_SERVICE,
                {"entity_id": speaker, "volume_level": round(volume / 100, 3)},
            )
            if volume_result.get("status") != "success":
                volume_error = volume_result.get("message")
                logger.error("could not set the volume of %s: %s", speaker, volume_error)
        result = call_service(TTS_DOMAIN, TTS_SERVICE, data)
    except Exception as e:  # missing/invalid home_assistant_config.json
        error_msg = str(e)
        logger.error("speak failed: %s", error_msg)
        write_log(LOG_TYPE, f"SPEAK by {person} ERROR: {error_msg}\n\"{message}\"")
        return _error("Speak failed", error_msg)

    # The spoken words go on their own line: they are the part worth reading, and
    # can run to MAX_MESSAGE_LENGTH characters. The log engine already supports a
    # multi-line entry body.
    write_log(
        LOG_TYPE,
        f"SPEAK by {person} on {speaker}"
        f"{' [' + language + ']' if language else ''}"
        f"{f' at {volume:g}%' if volume is not None else ''}"
        f" -> {result.get('status')}"
        f"{' (volume failed)' if volume_error else ''}\n"
        f"\"{message}\"",
    )
    reported = {**result, "id": person, "speaker": speaker, "tts": provider,
                "spoken": message, "volume": volume}
    if volume_error:
        reported["volume_error"] = volume_error
    return reported


if __name__ == "__main__":
    # Simple smoke test / demo — reaches Home Assistant only if configured.
    print("tts provider  ->", tts_entity())
    print("speakers      ->", all_speakers())
    print("no message    ->", speak({}))
    print("blank message ->", speak({"message": "   "}))
    print("too long      ->", speak({"message": "x" * (MAX_MESSAGE_LENGTH + 1)}))
    print("unknown       ->", speak({"message": "hi", "speaker": "nowhere"}))
    print("bad volume    ->", speak({"message": "hi", "volume": 0}))
    print("speak         ->", speak({"message": "Hello from Blink Server"}))
    print("speak loud    ->", speak({"message": "Hello", "volume": 80}))
