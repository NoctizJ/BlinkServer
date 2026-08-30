#!/usr/bin/env python3
"""Tests for the notifyPhone job (jobs/notify_phone.py), its Home Assistant
notification wrapper (jobs/home_assistant_notify.py), and the presence state
store (jobs/presence_state.py).

These tests mock out the HTTP call and the logging engine, and redirect the
presence file to a temp dir, so no real Home Assistant request is made and
nothing is written to the repo:

    python3 test_notify_phone.py
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.home_assistant_entities as he
import jobs.home_assistant_notify as han
import jobs.notify_phone as np
import jobs.presence_state as ps

FAKE_HA_CONFIG = {
    "HA_BASE_URL": "http://host:8123",
    "HA_API_KEY": "test-token",
}

# The notify target and panel entity live in home_assistant_entities.json now,
# so tests that reach notify_phone()/set_alarm() stub them rather than relying on
# whatever the repo's real entities file happens to hold.
FAKE_TARGET = "mobile_app_aisingioro"


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
            mock.patch.object(han, "notify_target", return_value=FAKE_TARGET), \
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
            mock.patch.object(han, "notify_target", return_value=FAKE_TARGET), \
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
        alarm.assert_called_once_with("arm", home=ps.DEFAULT_HOME)
        assert res["alarm"]["status"] == "success"
        assert res["notify"]["status"] == "success"

        alarm.reset_mock()
        np.arriving_home({"disarm": True})
        alarm.assert_called_once_with("disarm", home=ps.DEFAULT_HOME)
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


def test_per_home_title_and_message():
    """A "homes" block overrides the shared title/message for that home only."""
    print("Testing per-home title/message overrides...")
    file_cfg = {
        "title": "Shared title",
        "message": "Shared message for {id}",
        "homes": {
            "M": {"title": "M title", "message": "M message for {id}"},
            "T": {"title": "T title only"},
        },
    }
    with temp_presence_file(), \
            mock.patch.object(np, "load_event_text", return_value=file_cfg), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        # No home -> the shared text.
        np.leaving_home({"id": "Alex"})
        assert sent.call_args[0] == ("Shared title (A)", "Shared message for Alex"), sent.call_args[0]

        # Home M -> M's own text.
        np.leaving_home({"id": "Sam", "home": "M"})
        assert sent.call_args[0] == ("M title (A)", "M message for Sam"), sent.call_args[0]

        # Home T overrides only the title, so the shared message is inherited.
        np.leaving_home({"id": "Sam", "home": "T"})
        assert sent.call_args[0] == ("T title only (A)", "Shared message for Sam"), sent.call_args[0]

        # A home with no block at all falls back to the shared text.
        np.leaving_home({"id": "Sam", "home": "Z"})
        assert sent.call_args[0] == ("Shared title (A)", "Shared message for Sam"), sent.call_args[0]

        # The payload still beats the home block.
        np.leaving_home({"id": "Sam", "home": "M", "title": "Payload"})
        assert sent.call_args[0][0] == "Payload (A)", sent.call_args[0]
    print("  OK: per-home text used, partial blocks inherit, payload still wins")


def test_home_placeholder_is_filled():
    """"{home}" in a title/message is replaced with the home's name."""
    print("Testing {home} placeholder substitution...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        np.arriving_home({"id": "Sam", "home": "M",
                          "title": "{id} at {home}", "message": "{id} reached {home}"})
        assert sent.call_args[0] == ("Sam at M (D)", "Sam reached M"), sent.call_args[0]

        # With no home the placeholder resolves to the default.
        np.arriving_home({"id": "Alex", "message": "{id} reached {home}"})
        assert sent.call_args[0][1] == f"Alex reached {ps.DEFAULT_HOME}", sent.call_args[0]
    print("  OK: {home} replaced with the home's name")


def test_postfix_is_computed_per_home():
    """Somebody home in one house never disarms another house's title."""
    print("Testing the (A)/(D) postfix per home...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        # Sam is home in M. Home A is still empty.
        np.arriving_home({"id": "Sam", "home": "M", "title": "Arriving"})
        assert sent.call_args[0][0] == "Arriving (D)", sent.call_args[0]

        # Alex leaving the default home empties it, even though M is occupied.
        np.leaving_home({"id": "Alex", "home": ps.DEFAULT_HOME, "title": "Leaving"})
        assert sent.call_args[0][0] == "Leaving (A)", sent.call_args[0]

        # And leaving M empties M, even though it is a different house.
        np.leaving_home({"id": "Sam", "home": "M", "title": "Leaving"})
        assert sent.call_args[0][0] == "Leaving (A)", sent.call_args[0]

        # Directly, too: the store has Alex home in A only.
        ps.set_state("Alex", ps.STATE_HOME, event="arriving_home", home=ps.DEFAULT_HOME)
        assert np._title_postfix("leaving_home", "Sam", ps.DEFAULT_HOME) == np.POSTFIX_DISARM
        assert np._title_postfix("leaving_home", "Sam", "M") == np.POSTFIX_ARM
        # The home argument defaults to DEFAULT_HOME, as before multi-home.
        assert np._title_postfix("leaving_home", "Sam") == np.POSTFIX_DISARM
    print("  OK: postfix counts only the event's own home")


def test_per_home_arm_flag():
    """A "homes" block can override the arm/disarm flag, including to false."""
    print("Testing per-home arm/disarm flags...")
    file_cfg = {
        "title": "T",
        "message": "M",
        "arm": False,
        "homes": {"M": {"arm": True}, "T": {"arm": False}},
    }
    with temp_presence_file(), \
            mock.patch.object(np, "load_event_text", return_value=file_cfg), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}) as alarm, \
            mock.patch.object(np, "write_log"):
        # Shared config says don't arm.
        np.leaving_home({"id": "Alex"})
        alarm.assert_not_called()

        # Home M turns it on.
        np.leaving_home({"id": "Sam", "home": "M"})
        alarm.assert_called_once_with("arm", home="M")

        # Home T's explicit false is honored, not treated as unset.
        alarm.reset_mock()
        np.leaving_home({"id": "Sam", "home": "T"})
        alarm.assert_not_called()

        # The payload still beats the home block.
        np.leaving_home({"id": "Sam", "home": "T", "arm": True})
        alarm.assert_called_once_with("arm", home="T")
    print("  OK: per-home arm flag overrides the shared one, false honored")


def test_notify_persists_presence_in_the_named_home():
    """leaving/arriving write into the home the payload names."""
    print("Testing per-home presence persistence from notify...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        res = np.arriving_home({"id": "Sam", "home": "M"})
        assert res["home"] == "M", res
        assert res["presence"]["state"] == ps.STATE_HOME, res

        res = np.leaving_home({"id": "Sam"})   # no home -> the default
        assert res["home"] == ps.DEFAULT_HOME, res

        assert ps.get_state("Sam", home="M")["state"] == ps.STATE_HOME
        assert ps.get_state("Sam", home=ps.DEFAULT_HOME)["state"] == ps.STATE_AWAY
        assert set(ps.all_homes()) == {ps.DEFAULT_HOME, "M"}, ps.all_homes()
    print("  OK: presence written into the named home, default stays DEFAULT_HOME")


def test_odd_homes_block_falls_back():
    """A hand-mangled "homes" block is ignored rather than fatal."""
    print("Testing recovery from an odd homes block...")
    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(np, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(np, "write_log"):
        for bad in ("nope", ["nope"], {"M": "nope"}, {"M": ["nope"]}):
            with mock.patch.object(np, "load_event_text",
                                   return_value={"title": "Shared", "message": "Body", "homes": bad}):
                np.leaving_home({"id": "Sam", "home": "M"})
                assert sent.call_args[0] == ("Shared (A)", "Body"), (bad, sent.call_args[0])
    print("  OK: an unusable homes block falls back to the shared text")


def test_blink_webhooks_notify_the_phone():
    """/webhook/blink/arm and /disarm push a notification with the configured text."""
    print("Testing the blink arm/disarm notification...")
    import jobs.home_assistant_blink as hd

    with mock.patch.object(hd, "set_alarm", return_value={"status": "success"}) as alarm, \
            mock.patch.object(hd, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(hd, "load_event_text", return_value={}), \
            mock.patch.object(hd, "write_log"):
        # No config entry -> the built-in defaults.
        res = hd.arm()
        alarm.assert_called_once_with("arm", home=ps.DEFAULT_HOME)
        assert sent.call_args[0] == ("Blink Control (A)", ""), sent.call_args[0]
        assert res["notify"]["status"] == "success", res

        sent.reset_mock()
        hd.disarm()
        assert sent.call_args[0] == ("Blink Control (D)", ""), sent.call_args[0]

    # A configured title/message wins over the default.
    with mock.patch.object(hd, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(hd, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(hd, "load_event_text",
                              return_value={"title": "Panel armed", "message": "Away mode"}), \
            mock.patch.object(hd, "write_log"):
        hd.arm()
        assert sent.call_args[0] == ("Panel armed", "Away mode"), sent.call_args[0]
    print("  OK: blink webhooks notify, config text overrides the defaults")


def test_blink_notification_survives_a_broken_panel():
    """The notification is sent even when the panel call fails or raises."""
    print("Testing the blink notification against a broken panel...")
    import jobs.home_assistant_blink as hd

    # set_alarm raising (e.g. the HA config file is missing) must not lose it.
    with mock.patch.object(hd, "set_alarm", side_effect=ValueError("config gone")), \
            mock.patch.object(hd, "notify_phone", return_value={"status": "success"}) as sent, \
            mock.patch.object(hd, "load_event_text", return_value={}), \
            mock.patch.object(hd, "write_log"):
        res = hd.arm()
        assert res["status"] == "error" and "config gone" in res["message"], res
        assert sent.call_args[0][0] == "Blink Control (A)", sent.call_args[0]
        assert res["notify"]["status"] == "success", res

    # And a failing notification must not lose the arm result.
    with mock.patch.object(hd, "set_alarm", return_value={"status": "success"}), \
            mock.patch.object(hd, "notify_phone", side_effect=ValueError("no target")), \
            mock.patch.object(hd, "load_event_text", return_value={}), \
            mock.patch.object(hd, "write_log"):
        res = hd.disarm()
        assert res["status"] == "success", res
        assert res["notify"]["status"] == "error", res
    print("  OK: panel and notification failures are independent")


def test_leaving_arriving_send_exactly_one_notification():
    """set_alarm must not notify, or the notify webhooks would send two.

    Guards the boundary: the blink notification lives in the arm/disarm webhook
    handlers, not in the shared set_alarm() core that notify_phone also calls.
    """
    print("Testing that leaving/arriving still send one notification...")
    import jobs.home_assistant_blink as hd

    with temp_presence_file(), \
            mock.patch.object(np, "notify_phone", return_value={"status": "success"}) as np_sent, \
            mock.patch.object(hd, "notify_phone", return_value={"status": "success"}) as hd_sent, \
            mock.patch.object(hd, "load_config", return_value=FAKE_HA_CONFIG), \
            mock.patch.object(hd.requests, "post",
                              return_value=mock.Mock(status_code=200, text="{}")), \
            mock.patch.object(np, "write_log"), mock.patch.object(hd, "write_log"):
        # A real set_alarm call runs here (HTTP mocked), so if it notified we'd see it.
        np.leaving_home({"id": "Alex", "arm": True})
        assert np_sent.call_count == 1, np_sent.call_count
        assert hd_sent.call_count == 0, "set_alarm sent a second notification"
    print("  OK: one notification per leaving/arriving event")


def test_blink_notify_switches():
    """The blink notification is gated per action and by the master switch."""
    print("Testing the blink notification switches...")
    import tempfile
    import jobs.home_assistant_blink as hd

    with tempfile.TemporaryDirectory() as tmp:
        switch_file = Path(tmp) / "notify_switches.json"
        with mock.patch.object(hd, "SWITCH_FILE", switch_file), \
                mock.patch.object(hd, "set_alarm", return_value={"status": "success"}) as alarm, \
                mock.patch.object(hd, "notify_phone", return_value={"status": "success"}) as sent, \
                mock.patch.object(hd, "load_event_text", return_value={}), \
                mock.patch.object(hd, "write_log"):
            # An action nobody has toggled yet is on, and auto-registers.
            hd.arm()
            assert sent.call_count == 1, sent.call_count
            assert hd.all_notify_enabled() == {"arm": True}, hd.all_notify_enabled()

            # Disabling one action silences it but still moves the panel.
            hd.set_notify_enabled_for("arm", False)
            sent.reset_mock()
            alarm.reset_mock()
            res = hd.arm()
            assert sent.call_count == 0, "a disabled switch still notified"
            assert res["notify"]["status"] == "skipped", res
            alarm.assert_called_once_with("arm", home=ps.DEFAULT_HOME)

            # The other action is unaffected.
            hd.disarm()
            assert sent.call_count == 1, sent.call_count

            # The master switch silences both.
            hd.set_notify_enabled_for("arm", True)
            sent.reset_mock()
            with mock.patch.object(hd, "master_enabled", return_value=False):
                res = hd.arm()
                assert sent.call_count == 0, "master switch off but still notified"
                assert res["notify"]["status"] == "skipped", res
                assert hd.MASTER_SWITCH in res["notify"]["message"], res
    print("  OK: per-action and master switches gate the notification, not the panel")


def test_notify_switches_file_is_shared():
    """Location and blink switches live in one file, in their own sections."""
    print("Testing the shared notify_switches.json...")
    import jobs.home_assistant_blink as hd
    import jobs.location_notify as ln

    assert hd.SWITCH_FILE == ln.SWITCH_FILE, (hd.SWITCH_FILE, ln.SWITCH_FILE)
    assert hd.SWITCH_FILE.name == "notify_switches.json", hd.SWITCH_FILE
    assert hd.SWITCH_SECTION == "blink_control", hd.SWITCH_SECTION
    # The location section is named after its notify_config.json event.
    assert ln.SWITCH_SECTION == ln.EVENT == "location_log", ln.SWITCH_SECTION
    # Both share the one master switch.
    assert hd.MASTER_SWITCH == ln.MASTER_SWITCH == "notify_phone"

    # The shipped file has both sections.
    shipped = json.loads(
        (Path(__file__).parent.parent / "configs" / "notify_switches.json").read_text())
    assert "location_log" in shipped, shipped
    assert shipped["blink_control"] == {"arm": True, "disarm": True}, shipped
    print("  OK: one file, two sections, one master switch")


def test_ha_feature_switches_gate_both_integrations():
    """home_assistant_switches.json decides whether we talk to HA at all."""
    print("Testing the Home Assistant feature switches...")
    import jobs.home_assistant_blink as hd
    import jobs.home_assistant_switches as hs

    with tempfile.TemporaryDirectory() as tmp:
        ha_file = Path(tmp) / "home_assistant_switches.json"
        with mock.patch.object(hs, "SWITCH_FILE", ha_file):
            # Unknown features count as on and auto-register.
            assert hs.enabled_for(hs.BLINK) is True
            assert hs.enabled_for(hs.NOTIFY) is True

            # blink off -> no panel request, and load_config never called.
            hs.set_enabled_for(hs.BLINK, False)
            with mock.patch.object(hd, "load_config") as load, \
                    mock.patch.object(hd.requests, "post") as post:
                res = hd.set_alarm("arm")
                assert res["status"] == "skipped", res
                assert hs.BLINK in res["message"], res
                load.assert_not_called()   # off before the config is even read
                post.assert_not_called()

            # notify off -> no notification request, config never read.
            hs.set_enabled_for(hs.NOTIFY, False)
            with mock.patch.object(han, "_load_ha_config") as load, \
                    mock.patch.object(han.requests, "post") as post:
                res = han.notify_phone("T", "M")
                assert res["status"] == "skipped", res
                assert hs.NOTIFY in res["message"], res
                load.assert_not_called()
                post.assert_not_called()

            # The two are independent: alarm back on, notify still off.
            hs.set_enabled_for(hs.BLINK, True)
            with mock.patch.object(hd, "load_config", return_value=FAKE_HA_CONFIG), \
                    mock.patch.object(hd, "panel_entity",
                                      return_value="alarm_control_panel.test"), \
                    mock.patch.object(hd, "write_log"), \
                    mock.patch.object(hd.requests, "post",
                                      return_value=mock.Mock(status_code=200, text="{}")) as post:
                assert hd.set_alarm("arm")["status"] == "success"
                post.assert_called_once()
            with mock.patch.object(han, "_load_ha_config", return_value=FAKE_HA_CONFIG):
                assert han.notify_phone("T", "M")["status"] == "skipped"

            # An invalid action is still an error, not silently skipped.
            hs.set_enabled_for(hs.BLINK, False)
            try:
                hd.set_alarm("explode")
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError for a bad action")
    print("  OK: each HA feature gates its own integration, before config is read")


def test_ha_notify_switch_covers_every_notification():
    """The notify feature switch reaches every notifying job at once."""
    print("Testing that the HA notify switch covers all callers...")
    import jobs.home_assistant_blink as hd
    import jobs.home_assistant_switches as hs
    import jobs.location_notify as ln

    with tempfile.TemporaryDirectory() as tmp:
        ha_file = Path(tmp) / "home_assistant_switches.json"
        notify_file = Path(tmp) / "notify_switches.json"
        # Every switch file is redirected, so nothing is written to the repo.
        with mock.patch.object(hs, "SWITCH_FILE", ha_file), \
                mock.patch.object(ln, "SWITCH_FILE", notify_file), \
                mock.patch.object(hd, "SWITCH_FILE", notify_file), \
                temp_presence_file(), \
                mock.patch.object(han, "_load_ha_config", return_value=FAKE_HA_CONFIG), \
                mock.patch.object(han.requests, "post") as post, \
                mock.patch.object(np, "set_alarm", return_value={"status": "skipped"}), \
                mock.patch.object(hd, "set_alarm", return_value={"status": "skipped"}), \
                mock.patch.object(np, "write_log"), mock.patch.object(hd, "write_log"):
            hs.set_enabled_for(hs.NOTIFY, False)

            # leaving/arriving, the blink webhooks, and a logged position.
            assert np.leaving_home({"id": "Alex"})["notify"]["status"] == "skipped"
            assert np.arriving_home({"id": "Alex"})["notify"]["status"] == "skipped"
            assert hd.arm()["notify"]["status"] == "skipped"
            assert hd.disarm()["notify"]["status"] == "skipped"
            entry = {"latitude": 1.0, "longitude": 2.0, "time": "now"}
            assert ln.notify_location("Alex", entry)["status"] == "skipped"

            post.assert_not_called()  # not one HTTP request from any of them
    print("  OK: one switch silences leaving/arriving, blink, and location")


def test_entity_references():
    """Entity ids come from home_assistant_entities.json, and nowhere else."""
    print("Testing entity references...")
    import jobs.home_assistant_blink as hd

    with tempfile.TemporaryDirectory() as tmp:
        entities = Path(tmp) / "home_assistant_entities.json"

        with mock.patch.object(he, "CONFIG_FILE", str(entities)):
            entities.write_text(json.dumps({
                "blink": {
                    "panel_AMS": "alarm_control_panel.ams",
                    "panel_M": "alarm_control_panel.cabin",
                },
                "notify": {"target": "mobile_app_phone"},
                "lutron": {"lights": {"k": "light.k"}, "scenes": {}},
            }), encoding="utf-8")

            # The panel key carries the home name, defaulting to DEFAULT_HOME.
            assert hd.panel_entity() == "alarm_control_panel.ams"
            assert hd.panel_entity("M") == "alarm_control_panel.cabin"
            assert han.notify_target() == "mobile_app_phone"
            assert he.aliases("lutron", "lights") == {"k": "light.k"}

            # A home with no panel is a clear error naming the key it wanted.
            try:
                hd.panel_entity("nowhere")
            except ValueError as e:
                assert "panel_nowhere" in str(e), e
                assert "home_assistant_entities.json" in str(e), e
            else:
                raise AssertionError("expected ValueError for an unconfigured home")

            # Nothing falls back to the old home_assistant_config.json keys.
            entities.write_text(json.dumps({"blink": {}, "notify": {}}), encoding="utf-8")
            assert not hasattr(he, "LEGACY_CONFIG_FILE"), "legacy fallback still present"
            for call in (hd.panel_entity, han.notify_target):
                try:
                    call()
                except ValueError as e:
                    assert "home_assistant_entities.json" in str(e), e
                    assert "HA_ENTITY_ID" not in str(e) and "HA_NOTIFY_TARGET" not in str(e), e
                else:
                    raise AssertionError(f"expected ValueError from {call.__name__}")

            # A malformed entities file is not fatal.
            entities.write_text("{not json", encoding="utf-8")
            assert he.load_entities() == {}
            assert he.aliases("lutron", "lights") == {}
    print("  OK: panel keyed per home, no legacy fallback anywhere")

def test_connection_config_no_longer_demands_entities():
    """HA_ENTITY_ID / HA_NOTIFY_TARGET are no longer required config fields."""
    print("Testing the slimmed connection config...")
    import jobs.home_assistant_api as ha_api
    import jobs.home_assistant_blink as hd

    assert ha_api.REQUIRED_FIELDS == ("HA_BASE_URL", "HA_API_KEY"), ha_api.REQUIRED_FIELDS

    # Both loaders accept a config holding only the connection fields.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "home_assistant_config.json"
        path.write_text(json.dumps(FAKE_HA_CONFIG), encoding="utf-8")
        with mock.patch.object(hd, "os") as fake_os:
            fake_os.path.join.return_value = str(path)
            fake_os.path.dirname.return_value = tmp
            assert hd.load_config() == FAKE_HA_CONFIG
        with mock.patch.object(han, "CONFIG_FILE", str(path)):
            assert han._load_ha_config() == FAKE_HA_CONFIG

    # And the shipped example no longer advertises them.
    example = json.loads(
        (Path(__file__).parent.parent / "configs"
         / "home_assistant_config.example.json").read_text())
    assert set(example) == {"HA_BASE_URL", "HA_API_KEY"}, example

    # The shipped entities file uses the per-home panel key.
    entities = json.loads(
        (Path(__file__).parent.parent / "configs"
         / "home_assistant_entities.json").read_text())
    assert f"panel_{ps.DEFAULT_HOME}" in entities["blink"], entities["blink"]
    print("  OK: connection config is URL + token only")


def test_panel_is_routed_per_home():
    """Each home arms its own panel, from blink.panel_<home>."""
    print("Testing per-home panel routing...")
    import jobs.home_assistant_blink as hd

    with tempfile.TemporaryDirectory() as tmp:
        entities = Path(tmp) / "home_assistant_entities.json"
        entities.write_text(json.dumps({
            "blink": {
                f"panel_{ps.DEFAULT_HOME}": "alarm_control_panel.ams",
                "panel_M": "alarm_control_panel.cabin",
            },
            "notify": {"target": FAKE_TARGET},
        }), encoding="utf-8")

        def entity_of(post):
            return post.call_args[1]["json"]["entity_id"]

        with temp_presence_file(), \
                mock.patch.object(he, "CONFIG_FILE", str(entities)), \
                mock.patch.object(hd, "load_config", return_value=FAKE_HA_CONFIG), \
                mock.patch.object(hd.requests, "post",
                                  return_value=mock.Mock(status_code=200, text="{}")) as post, \
                mock.patch.object(hd, "_notify_blink", return_value={"status": "skipped"}), \
                mock.patch.object(np, "notify_phone", return_value={"status": "success"}), \
                mock.patch.object(np, "write_log"), mock.patch.object(hd, "write_log"):

            # /webhook/blink/* honours an optional "home".
            res = hd.arm({"home": "M"})
            assert entity_of(post) == "alarm_control_panel.cabin", post.call_args
            assert res["home"] == "M", res

            hd.disarm({})                      # no home -> the default
            assert entity_of(post) == "alarm_control_panel.ams", post.call_args

            # leaving/arriving arm the panel of the home the event belongs to.
            post.reset_mock()
            res = np.leaving_home({"id": "Sam", "home": "M", "arm": True})
            assert entity_of(post) == "alarm_control_panel.cabin", post.call_args
            assert res["alarm"]["status"] == "success", res

            res = np.arriving_home({"id": "Alex", "disarm": True})   # default home
            assert entity_of(post) == "alarm_control_panel.ams", post.call_args

            # The legacy run() handler routes too.
            hd.run({"action": "arm", "home": "M"})
            assert entity_of(post) == "alarm_control_panel.cabin", post.call_args

            # A home with no panel is an error, and arms nothing.
            post.reset_mock()
            res = hd.arm({"home": "nowhere"})
            assert res["status"] == "error", res
            assert "panel_nowhere" in res["message"], res
            assert post.call_count == 0, "an unconfigured home still armed a panel"

            # ...and through leaving/arriving it is reported, not raised.
            res = np.leaving_home({"id": "Sam", "home": "nowhere", "arm": True})
            assert res["alarm"]["status"] == "error", res
            assert "panel_nowhere" in res["alarm"]["message"], res
    print("  OK: each home arms its own panel; an unknown home arms nothing")


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
        assert ps.load_state() == {"homes": {}, "last_modified": None}
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
    test_per_home_title_and_message()
    test_home_placeholder_is_filled()
    test_postfix_is_computed_per_home()
    test_per_home_arm_flag()
    test_notify_persists_presence_in_the_named_home()
    test_odd_homes_block_falls_back()
    test_blink_webhooks_notify_the_phone()
    test_blink_notification_survives_a_broken_panel()
    test_leaving_arriving_send_exactly_one_notification()
    test_blink_notify_switches()
    test_notify_switches_file_is_shared()
    test_ha_feature_switches_gate_both_integrations()
    test_ha_notify_switch_covers_every_notification()
    test_entity_references()
    test_connection_config_no_longer_demands_entities()
    test_panel_is_routed_per_home()
    test_presence_is_persisted_per_person()
    test_presence_store_survives_a_corrupt_file()
    print("\nAll notifyPhone tests passed!")
