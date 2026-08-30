#!/usr/bin/env python3
"""Two-level on/off switches for Blink Server.

Several features are gated by the same pair of switches:

  * a **master** switch — one job's entry in ``configs/job_switches.json``, which
    turns the whole feature on or off (and is toggled by the ``/jobs`` endpoints);
  * a **per-key** switch — one entry per log type, per person, per action, ... in
    the feature's own JSON file, so parts of a feature can be turned off alone.

Something happens only when BOTH are enabled. The logging engine uses this for
its log types (``log_switches.json``, section ``"types"``), and the notification
jobs for their people and actions (``notify_switches.json``, sections
``"location_log"`` and ``"blink_control"``).

Files whose name ends in ``_switches.json`` are runtime toggle state: entries
appear in them automatically and may be flipped over HTTP. Files ending in
``_config.json`` are hand-written configuration.

A switch file looks like::

    {
      "location_log": { "娜": true, "Alex": false },
      "last_modified": "2026-08-18 21:04:11.221"
    }

A key that has never been seen is auto-registered as enabled the first time it
is checked, so it shows up in the file and can be turned off later.

Usage:
    from jobs.switches import master_enabled, is_enabled, set_enabled

    master_enabled("notify_phone", JOB_SWITCHES_PATH)  # the whole feature
    is_enabled(SWITCH_FILE, "location_log", "娜")       # one person
    set_enabled(SWITCH_FILE, "location_log", "娜", False)
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The repo root is the parent of the jobs/ folder that holds this module.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the master switches live: job_switches.json's "jobs" section.
JOB_SWITCHES_PATH = REPO_ROOT / "configs" / "job_switches.json"
JOBS_SECTION = "jobs"


def _now() -> str:
    """Modification timestamp, in the format the config files already use."""
    return str(datetime.datetime.now())


def load_switches(path, section: str, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load a switch file, falling back to ``fallback`` (or an empty section).

    A missing file is normal (nothing has been toggled yet). A corrupt or
    unexpectedly shaped file is logged and treated as empty rather than raising,
    so one bad file cannot take an endpoint down.
    """
    default = dict(fallback) if fallback else {section: {}, "last_modified": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s (%s); treating every switch as unset", path, e)
        return default

    if not isinstance(store, dict) or not isinstance(store.get(section), dict):
        logger.error("Unexpected structure in %s; treating every switch as unset", path)
        return default
    return store


def save_switches(path, store: Dict[str, Any]) -> None:
    """Persist a switch file, stamping the modification time.

    ``ensure_ascii=False`` keeps non-ASCII keys (e.g. "娜") readable in the file
    rather than escaped as ``\\u5a1c``.
    """
    store["last_modified"] = _now()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def all_switches(path, section: str) -> Dict[str, bool]:
    """Return every switch in a file's section, keyed by name."""
    return load_switches(path, section).get(section) or {}


def is_enabled(path, section: str, key: str) -> bool:
    """Return whether one key's switch is on, registering it if it is new.

    Unknown keys default to enabled and are written to the file, so they can be
    found and turned off later.
    """
    store = load_switches(path, section)
    switches = store.setdefault(section, {})
    if key not in switches:
        switches[key] = True
        save_switches(path, store)
    return bool(switches[key])


def set_enabled(path, section: str, key: str, enabled: bool) -> bool:
    """Turn one key's switch on or off, creating it if it does not exist yet.

    Returns the value that was written.
    """
    store = load_switches(path, section)
    store.setdefault(section, {})[key] = bool(enabled)
    save_switches(path, store)
    return bool(enabled)


def master_enabled(job_name: str, path=None) -> bool:
    """Return a job's master switch from job_switches.json (unknown means on).

    ``path`` defaults to :data:`JOB_SWITCHES_PATH`, read at call time so a caller
    can point at its own copy of the file (the logging engine does) and so tests
    can redirect it.
    """
    return bool(all_switches(path or JOB_SWITCHES_PATH, JOBS_SECTION).get(job_name, True))


if __name__ == "__main__":
    # Simple smoke test / demo — writes to a throwaway file.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        demo = Path(tmp) / "demo_switches.json"
        print("unknown key   ->", is_enabled(demo, "location_log", "娜"), "(auto-registered)")
        print("after disable ->", set_enabled(demo, "location_log", "娜", False))
        print("is_enabled    ->", is_enabled(demo, "location_log", "娜"))
        print("all           ->", all_switches(demo, "location_log"))
        print("file          ->", demo.read_text(encoding="utf-8"))
    print("master 'log'  ->", master_enabled("log"))
