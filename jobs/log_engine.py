#!/usr/bin/env python3
"""Logging engine for Blink Server.

Appends pretty, searchable, timestamped entries to files under ``logs/``.
Entries are routed by type: ``blink`` entries go to ``logs/blink.log`` and
every other type goes to ``logs/default.log``.

Every log has a *type* (an arbitrary string). If the type is ``None`` (or
empty) the ``DEFAULT_TYPE`` is used instead. Whether a log is actually
written is gated by two switches:

  * The master ``"log"`` switch in ``job_config.json`` — turns ALL logging
    on/off. This is the master switch for every log type.
  * A per-type switch in ``log_config.json`` — turns a single type on/off.

A log entry is only written when BOTH switches are enabled. The first time a
new type is seen it is auto-registered in ``log_config.json`` (enabled by
default) so it can be toggled on/off later.

Usage:
    from jobs.log_engine import log

    log("blink", "Camera 1 detected motion")   # -> logs/blink.log
    log(None, "Something happened")             # -> logs/default.log
"""

import datetime
import json
from pathlib import Path

# The repo root is the parent of the jobs/ folder that holds this module.
REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"

JOB_CONFIG_PATH = REPO_ROOT / "job_config.json"
LOG_CONFIG_PATH = REPO_ROOT / "log_config.json"

# Fallback type used when a caller passes None / an empty type.
DEFAULT_TYPE = "default"

# The "blink" type gets its own file; every other type shares default.log.
BLINK_TYPE = "blink"

# Key inside job_config.json["jobs"] that acts as the master log switch.
MASTER_SWITCH = "log"

# Pretty separators drawn around each entry.
WIDTH = 80
OUTER_SEP = "=" * WIDTH
INNER_SEP = "-" * WIDTH


def _normalize_type(log_type):
    """Normalize a type; None / empty falls back to the default type."""
    return (str(log_type).strip() if log_type else "") or DEFAULT_TYPE


def load_log_config():
    """Load per-type log configuration, falling back to a sane default."""
    try:
        with open(LOG_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"types": {DEFAULT_TYPE: True}, "last_modified": None}


def save_log_config(config):
    """Persist per-type log configuration, stamping the modification time."""
    config["last_modified"] = str(datetime.datetime.now())
    with open(LOG_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_type_enabled_status(log_type):
    """Return whether a log type is enabled (unknown types default to on)."""
    log_type = _normalize_type(log_type)
    return load_log_config().get("types", {}).get(log_type, True)


def set_type_status(log_type, enabled):
    """Enable/disable a log type, creating it if it does not exist yet.

    Returns the normalized type name that was written.
    """
    log_type = _normalize_type(log_type)
    config = load_log_config()
    config.setdefault("types", {})[log_type] = enabled
    save_log_config(config)
    return log_type


def _master_switch_enabled():
    """Return the master log switch from job_config.json (defaults to on)."""
    try:
        with open(JOB_CONFIG_PATH) as f:
            job_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return True
    return job_config.get("jobs", {}).get(MASTER_SWITCH, True)


def _type_enabled(log_type):
    """Return whether a given type is enabled.

    Unknown types are auto-registered (enabled) so they show up in
    log_config.json and can be toggled off later.
    """
    config = load_log_config()
    types = config.setdefault("types", {})
    if log_type not in types:
        types[log_type] = True
        save_log_config(config)
    return types[log_type]


def _log_file_for(log_type):
    """Route a type to its log file.

    ``blink`` entries go to ``logs/blink.log``; every other type is written to
    ``logs/default.log``. Reads the current ``LOGS_DIR`` at call time so tests
    can redirect it.
    """
    filename = "blink.log" if log_type == BLINK_TYPE else "default.log"
    return LOGS_DIR / filename


def _format_entry(log_type, text, timestamp):
    """Format a single, self-contained, searchable log entry.

    Layout (a separator is drawn between every entry)::

        ================================================================
        [2026-07-11 09:15:23.123] [BLINK]
        ----------------------------------------------------------------
        <log text, may span multiple lines>

    Keeping the timestamp and ``[TYPE]`` on one line makes the file easy to
    grep, e.g. ``grep "\\[BLINK\\]" logs/blink.log``.
    """
    return (
        f"{OUTER_SEP}\n"
        f"[{timestamp}] [{log_type.upper()}]\n"
        f"{INNER_SEP}\n"
        f"{text}\n"
    )


def log(log_type, text):
    """Append a timestamped, typed entry to its type's log file.

    ``blink`` entries are written to ``logs/blink.log``; all other types go to
    ``logs/default.log``.

    Args:
        log_type: The log type/category. May be ``None`` (or empty), in which
            case ``DEFAULT_TYPE`` is used.
        text: The message to log.

    Returns:
        bool: ``True`` if the entry was written, ``False`` if it was
        suppressed by the master switch or the per-type switch.
    """
    # Normalize the type; None / empty falls back to the default type.
    log_type = _normalize_type(log_type)

    # Master switch: if all logging is disabled, do nothing.
    if not _master_switch_enabled():
        return False

    # Per-type switch: skip if this type is turned off.
    if not _type_enabled(log_type):
        return False

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    entry = _format_entry(log_type, str(text), timestamp)

    LOGS_DIR.mkdir(exist_ok=True)
    with open(_log_file_for(log_type), "a", encoding="utf-8") as f:
        f.write(entry)

    return True


if __name__ == "__main__":
    # Simple smoke test / demo.
    print("default type ->", log(None, "This log used the default type."))
    print("blink type   ->", log("blink", "Camera 1 detected motion in the driveway."))
    print("multiline     ->", log("blink", "Line one of the message.\nLine two of the message."))
    print("Wrote to:", _log_file_for("blink"), "and", _log_file_for("default"))
