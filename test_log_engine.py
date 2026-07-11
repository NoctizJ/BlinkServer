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

sys.path.insert(0, str(Path(__file__).parent))

from jobs import log_engine


def _setup_temp_env():
    """Redirect the engine at a fresh temp dir with both switches on."""
    tmp = Path(tempfile.mkdtemp(prefix="blink_log_test_"))
    log_engine.LOGS_DIR = tmp
    log_engine.LOG_FILE = tmp / "blink.log"
    log_engine.LOG_CONFIG_PATH = tmp / "log_config.json"
    log_engine.JOB_CONFIG_PATH = tmp / "job_config.json"

    # Master switch ON, plus two known types enabled.
    log_engine.JOB_CONFIG_PATH.write_text(json.dumps({"jobs": {"log": True}}))
    log_engine.LOG_CONFIG_PATH.write_text(
        json.dumps({"types": {"default": True, "blink": True}})
    )
    return tmp


def _read_log():
    return log_engine.LOG_FILE.read_text() if log_engine.LOG_FILE.exists() else ""


def _types():
    return json.loads(log_engine.LOG_CONFIG_PATH.read_text())["types"]


def test_default_type():
    """A None type falls back to the default type."""
    _setup_temp_env()
    assert log_engine.log(None, "hello default") is True
    body = _read_log()
    assert "[DEFAULT]" in body
    assert "hello default" in body
    print("✅ test_default_type")


def test_typed_entry():
    """A provided type is written (upper-cased) in the header."""
    _setup_temp_env()
    assert log_engine.log("blink", "camera motion") is True
    body = _read_log()
    assert "[BLINK]" in body
    assert "camera motion" in body
    print("✅ test_typed_entry")


def test_pretty_format():
    """Entries carry separators and a bracketed timestamp + type header."""
    _setup_temp_env()
    log_engine.log("blink", "formatted?")
    body = _read_log()
    assert "=" * 80 in body        # outer separator
    assert "-" * 80 in body        # inner separator
    assert body.count("[") >= 2    # [timestamp] and [TYPE]
    print("✅ test_pretty_format")


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
    assert "should be suppressed" not in _read_log()
    print("✅ test_type_switch_off")


def test_master_switch_off():
    """The master switch suppresses every type."""
    tmp = _setup_temp_env()
    (tmp / "job_config.json").write_text(json.dumps({"jobs": {"log": False}}))
    assert log_engine.log("default", "master off") is False
    assert "master off" not in _read_log()
    print("✅ test_master_switch_off")


def test_set_get_type_status():
    """set_type_status / get_type_enabled_status round-trip."""
    _setup_temp_env()
    log_engine.set_type_status("garage", False)
    assert log_engine.get_type_enabled_status("garage") is False
    log_engine.set_type_status("garage", True)
    assert log_engine.get_type_enabled_status("garage") is True
    print("✅ test_set_get_type_status")


def main():
    tests = [
        test_default_type,
        test_typed_entry,
        test_pretty_format,
        test_auto_register_new_type,
        test_type_switch_off,
        test_master_switch_off,
        test_set_get_type_status,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} log engine tests passed ✅")


if __name__ == "__main__":
    main()
