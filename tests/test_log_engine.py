#!/usr/bin/env python3
"""Simple tests for the logging engine (jobs/log_engine.py).

Runs without any third-party dependencies:

    python3 test_log_engine.py

Each test points the engine at a fresh temporary directory, so your real
logs/ folder and *_config.json files are never touched.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jobs import log_engine


def _setup_temp_env():
    """Redirect the engine at a fresh temp dir with both switches on."""
    tmp = Path(tempfile.mkdtemp(prefix="blink_log_test_"))
    log_engine.LOGS_DIR = tmp
    log_engine.LOG_CONFIG_PATH = tmp / "log_config.json"
    log_engine.JOB_CONFIG_PATH = tmp / "job_config.json"

    # Master switch ON, plus two known types enabled.
    log_engine.JOB_CONFIG_PATH.write_text(json.dumps({"jobs": {"log": True}}))
    log_engine.LOG_CONFIG_PATH.write_text(
        json.dumps({"types": {"default": True, "blink": True}})
    )
    return tmp


def _read(log_type):
    """Read the log file a given type routes to."""
    path = log_engine._log_file_for(log_type)
    return path.read_text() if path.exists() else ""


def _types():
    return json.loads(log_engine.LOG_CONFIG_PATH.read_text())["types"]


def test_default_type():
    """A None type falls back to the default type."""
    _setup_temp_env()
    assert log_engine.log(None, "hello default") is True
    body = _read("default")
    assert "[DEFAULT]" in body
    assert "hello default" in body
    print("✅ test_default_type")


def test_typed_entry():
    """A provided type is written (upper-cased) in the header."""
    _setup_temp_env()
    assert log_engine.log("blink", "camera motion") is True
    body = _read("blink")
    assert "[BLINK]" in body
    assert "camera motion" in body
    print("✅ test_typed_entry")


def test_pretty_format():
    """Entries carry separators and a bracketed timestamp + type header."""
    _setup_temp_env()
    log_engine.log("blink", "formatted?")
    body = _read("blink")
    assert "=" * 80 in body        # outer separator
    assert "-" * 80 in body        # inner separator
    assert body.count("[") >= 2    # [timestamp] and [TYPE]
    print("✅ test_pretty_format")


def test_routing_by_type():
    """blink -> blink.log; upload -> upload.log; every other type -> default.log."""
    _setup_temp_env()
    log_engine.log("blink", "blink message")
    log_engine.log("upload", "upload message")    # own file
    log_engine.log("garage", "garage message")    # non-special -> default.log
    log_engine.log(None, "default message")        # default -> default.log

    blink = _read("blink")
    upload = _read("upload")
    default = _read("default")

    assert "blink message" in blink
    assert "upload message" in upload
    assert "garage message" in default
    assert "default message" in default
    # No cross-contamination between the files.
    assert "upload message" not in blink and "upload message" not in default
    assert "blink message" not in upload and "garage message" not in upload
    print("✅ test_routing_by_type")


def test_auto_register_new_type():
    """An unseen type is auto-added to log_config.json, enabled."""
    _setup_temp_env()
    assert "camera" not in _types()
    log_engine.log("camera", "new type")
    assert _types().get("camera") is True
    print("✅ test_auto_register_new_type")


def test_type_switch_off():
    """A disabled type suppresses the write."""
    _setup_temp_env()
    log_engine.set_type_status("blink", False)
    assert log_engine.log("blink", "should be suppressed") is False
    assert "should be suppressed" not in _read("blink")
    print("✅ test_type_switch_off")


def test_master_switch_off():
    """The master switch suppresses every type."""
    tmp = _setup_temp_env()
    (tmp / "job_config.json").write_text(json.dumps({"jobs": {"log": False}}))
    assert log_engine.log("default", "master off") is False
    assert "master off" not in _read("default")
    print("✅ test_master_switch_off")


def test_set_get_type_status():
    """set_type_status / get_type_enabled_status round-trip."""
    _setup_temp_env()
    log_engine.set_type_status("garage", False)
    assert log_engine.get_type_enabled_status("garage") is False
    log_engine.set_type_status("garage", True)
    assert log_engine.get_type_enabled_status("garage") is True
    print("✅ test_set_get_type_status")


def test_read_log_tail():
    """read_log(type, n) returns only the most recent n entries."""
    _setup_temp_env()
    for i in range(5):
        log_engine.log("blink", f"entry number {i}")
    out = log_engine.read_log("blink", entries=2)
    assert "entry number 4" in out and "entry number 3" in out
    assert "entry number 0" not in out
    assert out.count("=" * 80) == 2   # exactly two entry separators
    print("✅ test_read_log_tail")


def test_read_log_whole_and_missing():
    """read_log returns the whole file with no n, and '' for a missing file."""
    _setup_temp_env()
    assert log_engine.read_log("blink") == ""          # nothing written yet
    log_engine.log("default", "hello there")
    assert "hello there" in log_engine.read_log("default")  # entries=None -> whole
    assert log_engine.read_log("blink") == ""          # routed to a different file
    print("✅ test_read_log_whole_and_missing")


def test_read_not_gated_by_switches():
    """Reading must work even when the master and per-type switches are OFF.

    Writes are gated by job_config (master 'log') and log_config (per type);
    reads are not — you can always retrieve what's already on disk.
    """
    tmp = _setup_temp_env()
    log_engine.log("blink", "recorded while enabled")

    # Turn OFF the master switch and disable the blink type.
    (tmp / "job_config.json").write_text(json.dumps({"jobs": {"log": False}}))
    log_engine.set_type_status("blink", False)

    # New writes are now suppressed...
    assert log_engine.log("blink", "should be suppressed") is False
    # ...but reads still return what was written earlier.
    out = log_engine.read_log("blink")
    assert "recorded while enabled" in out
    assert "should be suppressed" not in out
    print("✅ test_read_not_gated_by_switches")


def main():
    tests = [
        test_default_type,
        test_typed_entry,
        test_pretty_format,
        test_routing_by_type,
        test_auto_register_new_type,
        test_type_switch_off,
        test_master_switch_off,
        test_set_get_type_status,
        test_read_log_tail,
        test_read_log_whole_and_missing,
        test_read_not_gated_by_switches,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} log engine tests passed ✅")


if __name__ == "__main__":
    main()
