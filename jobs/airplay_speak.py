#!/usr/bin/env python3
"""Text-to-speech straight to an AirPlay speaker, without Home Assistant.

Two webhooks (see config.json):

  - speak(payload) -> POST /webhook/speak/airplay
  - purge(payload) -> POST /webhook/speak/purge

Home Assistant could not discover the HomePod, so this talks to it directly:
macOS ``say`` renders the words to a WAV file, and pyatv streams that file to the
speaker over **RAOP** — the AirPlay *audio* protocol.

    text -> say -> audio/speak_<time>_<id>.wav -> pyatv RAOP stream_file -> speaker

Recordings are **kept** under ``audio/`` so you can hear what was said; trim
them with ``POST /webhook/speak/purge`` (``{"records": 5}``), which keeps the
newest and works like ``/webhook/location/purge``.

Why RAOP and not ``play_url``
-----------------------------
``atv.stream.play_url`` belongs to pyatv's **AirPlay** protocol and is the Apple
TV API — it pushes a URL at a device that then fetches it. A HomePod is an
AirPlay *audio* receiver, which pyatv exposes as the separate **RAOP** protocol,
whose playback method is ``stream_file``. Asking a HomePod for ``play_url``
raises ``NotSupportedError``.

Because ``stream_file`` sends the audio bytes directly, nothing needs to serve
the file over HTTP: no Flask, no ``0.0.0.0`` bind, no URL for the speaker to
fetch.

Requirements
------------
* ``pyatv`` installed (``pip install pyatv``). Without it the webhook returns a
  clear error instead of failing to import.
* macOS, for ``say``. The engine is only ever invoked as a list of arguments, so
  a message can never be interpreted as shell syntax.
* **The process must be on the speaker's LAN.** RAOP is a local protocol; this
  cannot work from a container or host that cannot reach the speaker directly.

Configuration
-------------
``configs/airplay_config.json`` maps a short alias to the speaker's address::

    {
      "speakers": { "bedroom": "10.0.0.155" },
      "voice": null,
      "rate": null
    }

A request names a speaker by alias or by raw address, and may be omitted when
exactly one is configured — the same rule :mod:`jobs.home_assistant_speak` uses::

    {"message": "Welcome home"}
    {"message": "Welcome home", "speaker": "bedroom"}
    {"message": "Welcome home", "speaker": "10.0.0.155", "volume": 40}

No credentials are needed for a HomePod whose RAOP service reports
``Pairing: NotNeeded``, which is why this config holds no secrets and is tracked.

Switches
--------
Gated by its **job** switch alone — ``airplay_speak`` in
``configs/job_switches.json``. There is deliberately no entry in
``home_assistant_switches.json``: this path never touches Home Assistant, so a
feature switch there would be a lie.

Every request is written to the **default** log with the requester and the words,
exactly like the Home Assistant speaker.
"""

import asyncio
import datetime
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.log_engine import DEFAULT_TYPE as LOG_TYPE
    from jobs.log_engine import log as write_log
    from jobs.presence_state import resolve_person
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import DEFAULT_TYPE as LOG_TYPE
    from log_engine import log as write_log
    from presence_state import resolve_person

# pyatv is an optional dependency: this is the only job that needs it, and the
# server must still start on a host without it.
try:
    import pyatv
    PYATV_IMPORT_ERROR = None
except ImportError as e:  # pragma: no cover - depends on the environment
    pyatv = None
    PYATV_IMPORT_ERROR = str(e)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = str(REPO_ROOT / "configs" / "airplay_config.json")

# Rendered speech is kept rather than thrown away, so you can go back and hear
# what the house actually said. Trim it with POST /webhook/speak/purge.
AUDIO_DIR = REPO_ROOT / "audio"

# Every file this job writes starts with this. Purge only ever considers files
# matching it, so a mis-set AUDIO_DIR can never delete something else.
AUDIO_PREFIX = "speak_"
AUDIO_SUFFIX = ".wav"
AUDIO_GLOB = f"{AUDIO_PREFIX}*{AUDIO_SUFFIX}"

# How many recordings POST /webhook/speak/purge keeps when not told otherwise.
DEFAULT_KEEP = 20

# Sections of airplay_config.json.
SPEAKERS_SECTION = "speakers"
VOICE_KEY = "voice"
RATE_KEY = "rate"

# Payload keys, matching jobs.home_assistant_speak so the two endpoints take the
# same request.
MESSAGE_KEYS = ("message", "text")
SPEAKER_KEYS = ("speaker", "host")

# Same cap as the Home Assistant speaker. Also bounds how long a single stream
# can hold the speaker: ~500 characters is well under a minute of speech.
MAX_MESSAGE_LENGTH = 500

# `say` writes a WAV that pyatv can stream. AIFF is `say`'s default and RAOP does
# not accept it, so the data format is given explicitly.
SAY_DATA_FORMAT = "LEI16@44100"

# Seconds to allow for synthesis and for the whole stream. A stream runs for the
# length of the audio, so this is generous rather than tight.
SAY_TIMEOUT = 30
STREAM_TIMEOUT = 120

# Speakers with a stream in flight, so two messages cannot fight over one
# device — RAOP holds an exclusive audio session.
_speaking: set = set()
_speaking_lock = threading.Lock()


def _error(error: str, message: str) -> Dict[str, Any]:
    """A webhook error result, reported rather than raised."""
    return {"status": "error", "error": error, "message": message}


def load_config() -> Dict[str, Any]:
    """Return airplay_config.json, or ``{}``.

    A missing or unreadable file is not fatal — a raw address still works, so the
    job degrades to "no aliases" rather than failing.
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.warning("airplay_config.json not found; only raw addresses will resolve")
        return {}
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in airplay_config.json: %s", e)
        return {}
    return config if isinstance(config, dict) else {}


def all_speakers() -> Dict[str, str]:
    """Return the alias -> address map, or ``{}``."""
    speakers = load_config().get(SPEAKERS_SECTION)
    return speakers if isinstance(speakers, dict) else {}


def _is_address(name: str) -> bool:
    """Whether a name is already an IP address rather than an alias."""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def resolve_speaker(name: Optional[str]) -> Optional[str]:
    """Turn an alias or raw address into an address, or ``None``.

    An IP address is used as-is. With no name at all, the single configured
    speaker is used — only unambiguous when exactly one is configured.
    """
    speakers = all_speakers()
    if name is None:
        return next(iter(speakers.values())) if len(speakers) == 1 else None
    if _is_address(name):
        return name
    return speakers.get(name)


def _first(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    """Return the first of ``keys`` the payload actually sets, stripped."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def _volume(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Read an optional 1-100 volume, or return an error text."""
    if "volume" not in payload:
        return None, None
    value = payload["volume"]
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None, f"volume {value!r} is not a number (expected 1-100)"
    if percent == 0:
        return None, "volume 0 would speak silently - use 1-100"
    if not 1 <= percent <= 100:
        return None, f"volume {percent:g} is out of range (expected 1-100)"
    return percent, None


def _safe(name: str) -> str:
    """Reduce a name to something safe inside a filename.

    Path separators and dots are dropped rather than escaped, so a person id can
    never walk out of AUDIO_DIR. Non-ASCII is kept — "娜" is a perfectly good
    filename character and the point is to stay readable.
    """
    cleaned = re.sub(r"[\\/\0.\s]+", "_", str(name)).strip("_")
    return cleaned[:40] or "unknown"


def audio_path(person: str) -> Path:
    """Return the path for a new recording, named so it sorts chronologically."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return AUDIO_DIR / f"{AUDIO_PREFIX}{stamp}_{_safe(person)}{AUDIO_SUFFIX}"


def all_recordings() -> list:
    """Return this job's recordings, newest first."""
    if not AUDIO_DIR.is_dir():
        return []
    files = [f for f in AUDIO_DIR.glob(AUDIO_GLOB) if f.is_file()]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _synthesize(message: str, person: str, voice: Optional[str],
                rate: Optional[Any]) -> Path:
    """Render text to a WAV under ``audio/`` with macOS ``say``.

    ``say`` is invoked with an argument list, never a shell string, so nothing in
    the message can be read as shell syntax.
    """
    AUDIO_DIR.mkdir(exist_ok=True)
    target = audio_path(person)
    path = str(target)

    command = ["say", "-o", path, "--data-format=" + SAY_DATA_FORMAT]
    if voice:
        command += ["-v", str(voice)]
    if rate:
        command += ["-r", str(rate)]
    command += ["--", message]

    result = subprocess.run(command, capture_output=True, text=True, timeout=SAY_TIMEOUT)
    if result.returncode != 0:
        target.unlink(missing_ok=True)   # a half-written file is not worth keeping
        raise RuntimeError(f"say failed ({result.returncode}): {result.stderr.strip()}")
    return target


async def _stream(address: str, audio: Path, volume: Optional[float]) -> None:
    """Stream a file to an AirPlay speaker over RAOP."""
    loop = asyncio.get_running_loop()
    # A unicast scan of the one address: no multicast, and it returns the device
    # with its services so RAOP is picked up without hard-coding a port or id.
    found = await pyatv.scan(loop, hosts=[address], timeout=5)
    if not found:
        raise RuntimeError(f"no AirPlay device answered at {address}")

    atv = await pyatv.connect(found[0], loop)
    try:
        if volume is not None:
            await atv.audio.set_volume(volume)
        await atv.stream.stream_file(str(audio))
    finally:
        atv.close()


def _run_speak(
    address: str,
    message: str,
    person: str,
    volume: Optional[float],
    voice: Optional[str],
    rate: Optional[Any],
) -> None:
    """Synthesize, stream, log, and clean up. Runs on a background thread.

    Nothing raises out of here — a thread that died silently would leave the
    address stuck in ``_speaking`` and a temporary file on disk.
    """
    audio = None
    try:
        audio = _synthesize(message, person, voice, rate)
        asyncio.run(asyncio.wait_for(_stream(address, audio, volume), STREAM_TIMEOUT))
        write_log(
            LOG_TYPE,
            f"AIRPLAY SPEAK by {person} on {address}"
            f"{f' at {volume:g}%' if volume is not None else ''}"
            f" -> success [{audio.name}]\n"
            f'"{message}"',
        )
    except Exception as e:
        logger.exception("airplay speak on %s failed", address)
        write_log(
            LOG_TYPE,
            f"AIRPLAY SPEAK by {person} on {address} ERROR: {e}"
            f"{f' [{audio.name}]' if audio is not None else ''}\n"
            f'"{message}"',
        )
    finally:
        # The recording is deliberately kept — purge trims it later.
        with _speaking_lock:
            _speaking.discard(address)


def speak(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/speak/airplay.

    Says ``message`` on an AirPlay speaker over RAOP, with no Home Assistant
    involved. Fields: ``message`` (required), ``id`` (who asked; defaults to the
    presence store's default person), ``speaker`` (alias or address, optional when
    only one is configured), ``volume`` as a percentage 1-100, and ``voice``.

    Returns as soon as the speech has been *started* — synthesis and streaming run
    on a background thread, because a stream lasts as long as the audio does and
    holding the request open would time out a Shortcut. The outcome goes to the
    default log.
    """
    logger.debug("airplay speak payload: %s", payload)

    if not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")

    if pyatv is None:
        return _error(
            "pyatv not installed",
            f"this job needs pyatv ('pip install pyatv'): {PYATV_IMPORT_ERROR}",
        )

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

    volume, error_msg = _volume(payload)
    if error_msg:
        return _error("Invalid volume", error_msg)

    name = _first(payload, SPEAKER_KEYS)
    address = resolve_speaker(name)
    if not address:
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
            f"{name!r} is not a known alias and is not an IP address. "
            f"Known speakers: {known}",
        )

    config = load_config()
    voice = _first(payload, ("voice",)) or config.get(VOICE_KEY)
    rate = config.get(RATE_KEY)

    # One stream per speaker: RAOP holds an exclusive audio session, so two at
    # once would fight over the device.
    with _speaking_lock:
        if address in _speaking:
            return _error(
                "Already speaking",
                f"{address} is already saying something - wait for it to finish",
            )
        _speaking.add(address)

    try:
        thread = threading.Thread(
            target=_run_speak,
            args=(address, message, person, volume, voice, rate),
            name=f"airplay-speak-{address}",
            daemon=True,
        )
        thread.start()
    except Exception as e:
        with _speaking_lock:
            _speaking.discard(address)
        return _error("Could not start speaking", str(e))

    return {
        "status": "started",
        "id": person,
        "speaker": address,
        "spoken": message,
        "volume": volume,
        "voice": voice,
        "message": f"Speaking on {address}",
    }



def _keep_count(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """Read the ``records`` field, or return an error text.

    Zero is allowed and empties the directory, matching
    :func:`jobs.location_webhook.purge`. A negative count is not.
    """
    if value is None:
        return None, None
    try:
        keep = int(value)
    except (TypeError, ValueError):
        return None, f"records {value!r} is not a whole number"
    if keep < 0:
        return None, f"records {keep} cannot be negative"
    return keep, None


def purge(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """Webhook handler for POST /webhook/speak/purge.

    Deletes old recordings from ``audio/``, keeping the newest::

        {}                  # keeps DEFAULT_KEEP (20)
        {"records": 5}      # keeps the 5 newest
        {"records": 0}      # removes them all

    Newest is decided by modification time, not by filename, so a hand-renamed
    file is still ordered correctly. Only files matching ``speak_*.wav`` are ever
    considered — nothing else in the directory can be deleted.

    A file that cannot be removed is counted in ``failed`` and reported rather
    than aborting the purge, so one locked file does not strand the rest.
    """
    logger.debug("airplay purge payload: %s", payload)

    if payload is not None and not isinstance(payload, dict):
        return _error("Invalid payload format", "Payload must be a JSON object")
    payload = payload if isinstance(payload, dict) else {}

    # Read the key directly rather than through _first(): _first() only accepts
    # scalars, so a list would have looked "absent" and silently purged to the
    # default instead of reporting a bad request.
    raw = payload.get("records", payload.get("keep"))
    keep, error_msg = _keep_count(raw)
    if error_msg:
        return _error("Invalid records", error_msg)
    if keep is None:
        keep = DEFAULT_KEEP

    recordings = all_recordings()          # newest first
    doomed = recordings[keep:]
    removed, failed = 0, []
    for recording in doomed:
        try:
            recording.unlink()
            removed += 1
        except OSError as e:
            failed.append(f"{recording.name}: {e}")
            logger.error("could not remove %s: %s", recording, e)

    kept = len(recordings) - removed
    result = {
        "status": "ok",
        "records": keep,
        "removed": removed,
        "kept": kept,
        "directory": str(AUDIO_DIR),
        "message": (f"Purged {removed} recording{'' if removed == 1 else 's'} beyond the "
                    f"{keep} most recent; {kept} kept."),
    }
    if failed:
        result["failed"] = failed
        result["message"] += f" {len(failed)} could not be removed."

    write_log(LOG_TYPE, f"AIRPLAY PURGE: {result['message']}")
    return result


if __name__ == "__main__":
    # Manual smoke test — needs pyatv, macOS `say`, and a reachable speaker.
    print("pyatv        ->", "installed" if pyatv else PYATV_IMPORT_ERROR)
    print("speakers     ->", all_speakers())
    print("recordings   ->", len(all_recordings()), "in", AUDIO_DIR)
    print("no message   ->", speak({}))
    print("unknown      ->", speak({"message": "hi", "speaker": "nowhere"}))
    print("bad volume   ->", speak({"message": "hi", "volume": 0}))
    started = speak({"message": "Hello from Blink Server"})
    print("speak        ->", started)
    if started.get("status") == "started":
        time.sleep(8)   # let the background thread finish before the process exits
    print("purge        ->", purge({"records": 5}))
