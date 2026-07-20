#!/usr/bin/env python3
"""Tests for the notifyPhone job (jobs/notify_phone.py) and its Home Assistant
notification wrapper (jobs/home_assistant_notify.py).

These tests mock out the HTTP call and the logging engine, so no real Home
Assistant request is made and nothing is written to the repo:

    python3 test_notify_phone.py
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.home_assistant_notify as han
import jobs.notify_phone as np

FAKE_HA_CONFIG = {
    "HA_BASE_URL": "http://host:8123",
    "HA_API_KEY": "test-token",
    "HA_NOTIFY_TARGET": "mobile_app_aisingioro",
}


def test_wrapper_posts_to_notify_service():
    """notify_phone() hits /api/services/notify/<target> with title/message."""
    print("Testing home_assistant_notify.notify_phone()...")
    with mock.patch.object(han, "_load_ha_config", return_value=FAKE_HA_CONFIG), \
            mock.patch.object(han.requests, "post") as post:
        post.return_value = mock.Mock(status_code=200, text="{}")
        result = han.notify_phone("API Test", "Hello from test")

    args, kwargs = post.call_args
    assert args[0] == "http://host:8123/api/services/notify/mobile_app_aisingioro", args[0]
    assert kwargs["json"] == {"title": "API Test", "message": "Hello from test"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert result["status"] == "success"
    print("  OK: posts to notify service with title, message, and bearer token")


def test_http_failure_is_reported_not_raised():
    """A non-2xx response returns an error dict rather than raising."""
    print("Testing notify_phone() HTTP failure handling...")
    with mock.patch.object(han, "_load_ha_config", return_value=FAKE_HA_CONFIG), \
            mock.patch.object(han.requests, "post") as post:
        post.return_value = mock.Mock(status_code=401, text="unauthorized")
        result = han.notify_phone("T", "M")
    assert result["status"] == "error"
    assert "401" in result["message"]
    print("  OK: HTTP error reported in the return value")


def test_config_precedence():
    """Resolution precedence is payload > notify_config.json > default."""
    print("Testing notifyPhone title/message resolution...")
    with mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        # From notify_config.json (defaults shipped in the repo).
        np.leaving_home({})
        title, message = sent.call_args[0]
        assert title and message, (title, message)

        # Payload overrides win.
        sent.reset_mock()
        np.arriving_home({"title": "Custom", "message": "Overridden"})
        assert sent.call_args[0] == ("Custom", "Overridden"), sent.call_args[0]
    print("  OK: config values used, payload overrides win")


def test_leaving_arms_arriving_disarms():
    """When the flag is on, leaving arms the panel and arriving disarms it.

    The flag is passed explicitly here so the test is deterministic regardless
    of the current notify_config.json values.
    """
    print("Testing arm-on-leaving / disarm-on-arriving...")
    with mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}) as alarm, \
            mock.patch.object(np, "write_log"):
        res = np.leaving_home({"arm": True})
        alarm.assert_called_once_with("arm")
        assert res["alarm"]["status"] == "success"
        assert res["notify"]["status"] == "success"

        alarm.reset_mock()
        np.arriving_home({"disarm": True})
        alarm.assert_called_once_with("disarm")
    print("  OK: leaving -> arm, arriving -> disarm")


def test_alarm_can_be_disabled_by_config():
    """An explicit false flag (via payload here) skips the alarm action but
    still sends the notification."""
    print("Testing arm/disarm disable flag...")
    with mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm") as alarm, \
            mock.patch.object(np, "write_log"):
        res = np.leaving_home({"arm": False})
        alarm.assert_not_called()
        assert res["alarm"]["status"] == "skipped"
        assert res["notify"]["status"] == "success"
    print("  OK: arm disabled -> set_alarm skipped, notification still sent")


if __name__ == "__main__":
    test_wrapper_posts_to_notify_service()
    test_http_failure_is_reported_not_raised()
    test_config_precedence()
    test_leaving_arms_arriving_disarms()
    test_alarm_can_be_disabled_by_config()
    print("\nAll notifyPhone tests passed!")
