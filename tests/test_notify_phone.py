#!/usr/bin/env python3
"""Tests for the notifyPhone job (jobs/notify_phone.py), its Home Assistant
notification wrapper (jobs/home_assistant_notify.py), and the presence state
store (jobs/presence_state.py).

These tests mock out the HTTP call and the logging engine, and redirect the
presence file to a temp dir, so no real Home Assistant request is made and
nothing is written to the repo:

    python3 test_notify_phone.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.home_assistant_notify as han
import jobs.notify_phone as np
import jobs.presence_state as ps

FAKE_HA_CONFIG = {
    "HA_BASE_URL": "http://host:8123",
    "HA_API_KEY": "test-token",
    "HA_NOTIFY_TARGET": "mobile_app_aisingioro",
}


class temp_presence_file:
    """Context manager pointing the presence store at a temp file."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(ps, "STATE_DIR", directory),
            mock.patch.object(ps, "STATE_FILE", directory / "presence.json"),
        ]
        for patch in self._patches:
            patch.start()
        return ps.STATE_FILE

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False


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
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        # From notify_config.json (defaults shipped in the repo).
        np.leaving_home({})
        title, message = sent.call_args[0]
        assert title and message, (title, message)

        # Payload overrides win (the title still gets the arm/disarm postfix).
        sent.reset_mock()
        np.arriving_home({"title": "Custom", "message": "Overridden"})
        assert sent.call_args[0] == ("Custom (D)", "Overridden"), sent.call_args[0]
    print("  OK: config values used, payload overrides win")


def test_title_postfix_tracks_who_is_left_at_home():
    """The title ends in "(A)" when everybody is away, "(D)" when somebody is home."""
    print("Testing the (A)/(D) title postfix...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        # Empty store: the first arrival puts somebody home -> disarm.
        res = np.arriving_home({"id": "Alex", "title": "Arriving home"})
        assert sent.call_args[0][0] == "Arriving home (D)", sent.call_args[0]
        assert res["title"] == "Arriving home (D)", res

        # Sam arrives too, so Alex leaving still leaves somebody in -> disarm.
        np.arriving_home({"id": "Sam"})
        res = np.leaving_home({"id": "Alex", "title": "Leaving home"})
        assert sent.call_args[0][0] == "Leaving home (D)", sent.call_args[0]
        assert res["title"] == "Leaving home (D)", res

        # Sam is the last one out -> arm.
        res = np.leaving_home({"id": "Sam", "title": "Leaving home"})
        assert sent.call_args[0][0] == "Leaving home (A)", sent.call_args[0]
        assert res["title"] == "Leaving home (A)", res

        # ...and their arrival flips it straight back to disarm.
        np.arriving_home({"id": "Sam", "title": "Arriving home"})
        assert sent.call_args[0][0] == "Arriving home (D)", sent.call_args[0]
    print("  OK: (A) when the house empties, (D) while anyone is home")


def test_postfix_uses_this_events_state_not_just_the_store():
    """The postfix reflects the household *after* the event, even before it is
    written — a lone person leaving arms, arriving disarms."""
    print("Testing the postfix against the event's own state...")
    with temp_presence_file(), \
            mock.patch.object(np, "write_log"):
        ps.set_state("Alex", ps.STATE_HOME, event="arriving_home")
        # Alex is the only one home, so their leaving empties the house.
        assert np._title_postfix("leaving_home", "Alex") == np.POSTFIX_ARM
        assert np._title_postfix("arriving_home", "Alex") == np.POSTFIX_DISARM

        # Somebody else being home keeps it disarmed whoever leaves.
        ps.set_state("Sam", ps.STATE_HOME, event="arriving_home")
        assert np._title_postfix("leaving_home", "Alex") == np.POSTFIX_DISARM

        # An event with no presence of its own gets no postfix.
        assert np._title_postfix("some_other_event", "Alex") == ""
    print("  OK: postfix computed from the post-event household state")


def test_postfix_survives_an_unreadable_presence_store():
    """A presence store that cannot be read costs the postfix, not the notification."""
    print("Testing postfix fallback on a presence failure...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "anyone_home", side_effect=OSError("disk gone")), \
            mock.patch.object(np, "write_log"):
        res = np.leaving_home({"title": "Leaving home"})
        assert sent.call_args[0][0] == "Leaving home", sent.call_args[0]
        assert res["notify"]["status"] == "success", res
    print("  OK: unreadable store -> plain title, notification still sent")


def test_leaving_arms_arriving_disarms():
    """When the flag is on, leaving arms the panel and arriving disarms it.

    The flag is passed explicitly here so the test is deterministic regardless
    of the current notify_config.json values.
    """
    print("Testing arm-on-leaving / disarm-on-arriving...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
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
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm") as alarm, \
            mock.patch.object(np, "write_log"):
        res = np.leaving_home({"arm": False})
        alarm.assert_not_called()
        assert res["alarm"]["status"] == "skipped"
        assert res["notify"]["status"] == "success"
    print("  OK: arm disabled -> set_alarm skipped, notification still sent")


def test_id_defaults_to_default_person():
    """A payload without "id" is attributed to the default person."""
    print("Testing the id payload key...")
    assert ps.resolve_person({"id": "Alex"}) == "Alex"
    assert ps.resolve_person({"id": "  Alex  "}) == "Alex"
    assert ps.resolve_person({"id": ""}) == ps.DEFAULT_PERSON
    assert ps.resolve_person({}) == ps.DEFAULT_PERSON
    assert ps.resolve_person(None) == ps.DEFAULT_PERSON
    assert ps.DEFAULT_PERSON == "娜"
    print("  OK: id read from the payload, missing/blank falls back to 娜")


def test_id_fills_message_placeholder():
    """"{id}" in a title/message is replaced with the person's name."""
    print("Testing {id} placeholder substitution...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        res = np.arriving_home({"id": "Alex", "message": "{id} just walked in"})
        assert sent.call_args[0][1] == "Alex just walked in", sent.call_args[0]
        assert res["person"] == "Alex"

        # No id -> the default person's name is substituted instead.
        sent.reset_mock()
        np.arriving_home({"message": "{id} just walked in"})
        assert sent.call_args[0][1] == f"{ps.DEFAULT_PERSON} just walked in", sent.call_args[0]
    print("  OK: {id} replaced with the person's name")


def test_presence_is_persisted_per_person():
    """Leaving marks a person away, arriving marks them home, in the JSON file."""
    print("Testing presence persistence...")
    with temp_presence_file() as state_file, \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        res = np.leaving_home({"id": "Alex"})
        assert res["presence"]["state"] == ps.STATE_AWAY, res["presence"]
        assert res["presence"]["event"] == "leaving_home", res["presence"]

        np.arriving_home({})  # no id -> 娜
        np.leaving_home({"id": "Alex"})
        np.arriving_home({"id": "Alex"})

        assert state_file.exists(), state_file
        people = ps.all_states()
        assert people["Alex"]["state"] == ps.STATE_HOME, people
        assert people[ps.DEFAULT_PERSON]["state"] == ps.STATE_HOME, people
        assert ps.get_state("Alex")["last_updated"], people
        assert ps.get_state("nobody") is None
        assert ps.anyone_home() is True
        assert ps.anyone_home({"Alex": ps.STATE_AWAY, ps.DEFAULT_PERSON: ps.STATE_AWAY}) is False
    print("  OK: each person's home/away state persisted to presence.json")


def test_presence_store_survives_a_corrupt_file():
    """A malformed presence file is reported and replaced, not fatal."""
    print("Testing presence store recovery from a bad file...")
    with temp_presence_file() as state_file:
        state_file.write_text("{not json", encoding="utf-8")
        assert ps.load_state() == {"people": {}, "last_modified": None}
        ps.set_state("Alex", ps.STATE_HOME, event="arriving_home")
        assert ps.get_state("Alex")["state"] == ps.STATE_HOME

        try:
            ps.set_state("Alex", "elsewhere")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for an invalid presence")
    print("  OK: corrupt file recovered, invalid presence rejected")


if __name__ == "__main__":
    test_wrapper_posts_to_notify_service()
    test_http_failure_is_reported_not_raised()
    test_config_precedence()
    test_title_postfix_tracks_who_is_left_at_home()
    test_postfix_uses_this_events_state_not_just_the_store()
    test_postfix_survives_an_unreadable_presence_store()
    test_leaving_arms_arriving_disarms()
    test_alarm_can_be_disabled_by_config()
    test_id_defaults_to_default_person()
    test_id_fills_message_placeholder()
    test_presence_is_persisted_per_person()
    test_presence_store_survives_a_corrupt_file()
    print("\nAll notifyPhone tests passed!")
