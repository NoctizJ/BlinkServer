#!/usr/bin/env python3
"""Tests for the Lutron job (jobs/home_assistant_lutron.py) and the shared Home Assistant API
caller (jobs/home_assistant_api.py).

The HTTP call, the logging engine, and every switch file are mocked or
redirected, so no real Home Assistant request is made and nothing is written to
the repo:

    python3 tests/test_lutron.py
"""

import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.home_assistant_api as ha_api
from jobs.text_format import display_width
import jobs.home_assistant_entities as he
import jobs.home_assistant_switches as hs
import jobs.home_assistant_lutron as lu

FAKE_HA_CONFIG = {
    "HA_BASE_URL": "http://host:8123",
    "HA_API_KEY": "test-token",
}

# What Home Assistant reports for the aliased entities. "bogus"/"scene.*" are
# deliberately absent so the status report has a stale alias to surface.
STATE_LIST = [
    {"entity_id": "light.kitchen_main", "state": "on",
     "attributes": {"brightness": 255, "friendly_name": "Kitchen Main"}},
    {"entity_id": "switch.hallway_lights", "state": "off",
     "attributes": {"friendly_name": "Hallway Lights"}},
    {"entity_id": "light.unrelated", "state": "on", "attributes": {}},
]

ENTITIES = {
    "lutron": {
        "lights": {
            "kitchen": "light.kitchen_main",
            "hallway": "switch.hallway_lights",
            "bogus": "scene.not_a_light",
        },
        "scenes": {
            "movie": "scene.movie_night",
            "bogus": "light.not_a_scene",
        },
    },
}


class lutron_env:
    """Context manager mocking HA config/HTTP and redirecting every switch file.

    Yields the mock standing in for ``requests.post``, so a test can inspect the
    exact service call that was made.
    """

    def __init__(self, entities=ENTITIES, status_code=200):
        self._entities = entities
        self._status = status_code

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        (directory / "home_assistant_entities.json").write_text(
            json.dumps(self._entities), encoding="utf-8")

        self.post = mock.Mock(return_value=mock.Mock(status_code=self._status, text="{}"))
        self.get = mock.Mock(return_value=mock.Mock(
            status_code=200, text="[]", json=lambda: list(STATE_LIST)))
        self._patches = [
            mock.patch.object(ha_api.requests, "get", self.get),
            mock.patch.object(he, "CONFIG_FILE", str(directory / "home_assistant_entities.json")),
            mock.patch.object(hs, "SWITCH_FILE", directory / "home_assistant_switches.json"),
            mock.patch.object(ha_api, "load_connection", return_value=FAKE_HA_CONFIG),
            mock.patch.object(ha_api.requests, "post", self.post),
            mock.patch.object(lu, "write_log"),
        ]
        for patch in self._patches:
            patch.start()
        return self.post

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False


def _called(post):
    """Return the (url, json body) of the single request that was made."""
    assert post.call_count == 1, f"expected 1 request, got {post.call_count}"
    args, kwargs = post.call_args
    return args[0], kwargs["json"]


def test_light_on_off_toggle():
    """A light resolves to the right service, with the alias mapped."""
    print("Testing light on/off/toggle...")
    with lutron_env() as post:
        res = lu.light({"light": "kitchen"})   # state defaults to on
        url, body = _called(post)
        assert url == "http://host:8123/api/services/light/turn_on", url
        assert body == {"entity_id": "light.kitchen_main"}, body
        assert res["status"] == "success", res

        for state, service in (("on", "turn_on"), ("off", "turn_off"),
                               ("toggle", "toggle"), ("OFF", "turn_off"),
                               ("true", "turn_on"), ("false", "turn_off")):
            post.reset_mock()
            lu.light({"light": "kitchen", "state": state})
            url, _ = _called(post)
            assert url.endswith(f"/light/{service}"), (state, url)
    print("  OK: on/off/toggle map to turn_on/turn_off/toggle, aliases resolve")


def test_brightness_is_a_percentage():
    """brightness is sent as brightness_pct, and validated 0-100."""
    print("Testing brightness...")
    with lutron_env() as post:
        lu.light({"light": "kitchen", "brightness": 40})
        url, body = _called(post)
        assert url.endswith("/light/turn_on"), url
        assert body == {"entity_id": "light.kitchen_main", "brightness_pct": 40.0}, body

        # Boundaries are fine, including 0.
        for value in (0, 100, "55", 12.5):
            post.reset_mock()
            res = lu.light({"light": "kitchen", "brightness": value})
            assert res["status"] == "success", (value, res)

        # Out of range and non-numeric are errors, and nothing is sent.
        for value in (-1, 101, 255, "loads", None, [40]):
            post.reset_mock()
            res = lu.light({"light": "kitchen", "brightness": value})
            assert res["error"] == "Invalid brightness", (value, res)
            assert post.call_count == 0, value
    print("  OK: brightness sent as brightness_pct, 0-100 enforced")


def test_transition_is_passed_through():
    """transition is forwarded in seconds, and validated."""
    print("Testing transition...")
    with lutron_env() as post:
        lu.light({"light": "kitchen", "state": "off", "transition": 2})
        _, body = _called(post)
        assert body == {"entity_id": "light.kitchen_main", "transition": 2.0}, body

        post.reset_mock()
        lu.scene({"scene": "movie", "transition": 3})
        url, body = _called(post)
        assert url.endswith("/scene/turn_on"), url
        assert body == {"entity_id": "scene.movie_night", "transition": 3.0}, body

        for value in (-1, "slowly"):
            post.reset_mock()
            res = lu.light({"light": "kitchen", "transition": value})
            assert res["error"] == "Invalid transition", (value, res)
            assert post.call_count == 0, value
    print("  OK: transition forwarded, negatives and non-numbers rejected")


def test_scene_activates_only():
    """A scene is activated via scene.turn_on; there is no off or toggle."""
    print("Testing scene activation...")
    with lutron_env() as post:
        res = lu.scene({"scene": "movie"})
        url, body = _called(post)
        assert url == "http://host:8123/api/services/scene/turn_on", url
        assert body == {"entity_id": "scene.movie_night"}, body
        assert res["status"] == "success", res

        # A "state" on a scene is simply ignored — scenes only activate.
        post.reset_mock()
        lu.scene({"scene": "movie", "state": "off"})
        url, _ = _called(post)
        assert url.endswith("/scene/turn_on"), url
    print("  OK: scenes call scene.turn_on, nothing else")


def test_raw_entity_ids_work_without_an_alias():
    """Anything containing a dot is used as an entity id directly."""
    print("Testing raw entity ids...")
    with lutron_env() as post:
        lu.light({"light": "light.some_other_lamp"})
        _, body = _called(post)
        assert body == {"entity_id": "light.some_other_lamp"}, body

        post.reset_mock()
        lu.scene({"scene": "scene.unlisted"})
        _, body = _called(post)
        assert body == {"entity_id": "scene.unlisted"}, body

    print("  OK: raw entity ids resolve without an alias")


def test_domain_comes_from_the_entity_id():
    """A Lutron non-dim switch is a switch.* entity, so switch.* is called."""
    print("Testing domain derivation...")
    with lutron_env() as post:
        lu.light({"light": "hallway", "state": "on"})
        url, body = _called(post)
        assert url == "http://host:8123/api/services/switch/turn_on", url
        assert body == {"entity_id": "switch.hallway_lights"}, body

        post.reset_mock()
        lu.light({"light": "hallway", "state": "toggle"})
        url, _ = _called(post)
        assert url.endswith("/switch/toggle"), url

        # A switch cannot dim, so brightness is an error rather than dropped.
        post.reset_mock()
        res = lu.light({"light": "hallway", "brightness": 40})
        assert res["error"] == "Not dimmable", res
        assert post.call_count == 0
    print("  OK: switch entities call switch.*, and reject brightness")


def test_wrong_domain_for_the_endpoint():
    """A scene sent to the light endpoint (or vice versa) is an error."""
    print("Testing endpoint/domain mismatches...")
    with lutron_env() as post:
        res = lu.light({"light": "bogus"})        # -> scene.not_a_light
        assert res["error"] == "Wrong entity domain", res
        assert "scene" in res["message"], res

        res = lu.scene({"scene": "bogus"})        # -> light.not_a_scene
        assert res["error"] == "Wrong entity domain", res
        assert post.call_count == 0
    print("  OK: mismatched domains reported, nothing sent")


def test_bad_payloads():
    """Missing names, unknown aliases, and bad states are reported not raised."""
    print("Testing payload validation...")
    with lutron_env() as post:
        assert lu.light({})["error"] == "Missing light"
        assert lu.scene({})["error"] == "Missing scene"
        assert lu.light("nope")["error"] == "Invalid payload format"
        assert lu.scene(None)["error"] == "Invalid payload format"

        unknown = lu.light({"light": "nowhere"})
        assert unknown["error"] == "Unknown light", unknown
        assert "kitchen" in unknown["message"], unknown   # lists known aliases

        assert lu.scene({"scene": "nowhere"})["error"] == "Unknown scene"

        bad = lu.light({"light": "kitchen", "state": "sideways"})
        assert bad["error"] == "Invalid state", bad

        # JSON false/true must mean off/on, not "absent" -> a bare `or` would
        # have turned the light ON when asked to turn it off.
        for state, service in ((False, "turn_off"), (True, "turn_on"),
                               ("false", "turn_off"), ("", "turn_on"), (None, "turn_on")):
            post.reset_mock()
            res = lu.light({"light": "kitchen", "state": state})
            assert res["status"] == "success", (state, res)
            assert _called(post)[0].endswith(f"/light/{service}"), (state, _called(post)[0])

        # Bare integers are not a documented spelling and stay rejected.
        for state in (0, 1, 2):
            post.reset_mock()
            assert lu.light({"light": "kitchen", "state": state})["error"] == "Invalid state", state
            assert post.call_count == 0, state

        # brightness with an explicit off is contradictory.
        conflict = lu.light({"light": "kitchen", "state": "off", "brightness": 50})
        assert conflict["error"] == "Conflicting request", conflict

    print("  OK: bad payloads rejected; JSON false means off, not on")


def test_missing_alias_file_still_allows_entity_ids():
    """No entities file means no aliases, but raw entity ids still work."""
    print("Testing a missing alias file...")
    with lutron_env() as post:
        with mock.patch.object(he, "CONFIG_FILE", "/nonexistent/home_assistant_entities.json"):
            assert lu.all_aliases(lu.LIGHTS_SECTION) == {}
            res = lu.light({"light": "light.raw"})
            assert res["status"] == "success", res
            _, body = _called(post)
            assert body == {"entity_id": "light.raw"}, body

            post.reset_mock()
            unknown = lu.light({"light": "kitchen"})
            assert unknown["error"] == "Unknown light", unknown
            assert "(none configured)" in unknown["message"], unknown

    # A corrupt file behaves the same way rather than raising.
    with lutron_env() as post:
        with mock.patch.object(he, "section", return_value={"lights": "nope"}):
            assert lu.all_aliases(lu.LIGHTS_SECTION) == {}
            assert lu.light({"light": "light.raw"})["status"] == "success"
    print("  OK: missing/corrupt alias file degrades to entity ids only")


def test_lutron_feature_switch():
    """The lutron HA feature switch stops both endpoints."""
    print("Testing the lutron feature switch...")
    with lutron_env() as post:
        hs.set_enabled_for(hs.LUTRON, False)

        for res in (lu.light({"light": "kitchen"}), lu.scene({"scene": "movie"})):
            assert res["status"] == "skipped", res
            assert "lutron" in res["message"], res
        assert post.call_count == 0, "a disabled feature still called Home Assistant"

        hs.set_enabled_for(hs.LUTRON, True)
        assert lu.light({"light": "kitchen"})["status"] == "success"
        assert post.call_count == 1

    # The feature is registered as a real one, so /ha/lutron/* validates.
    assert hs.LUTRON == "lutron"
    assert hs.LUTRON in hs.FEATURES, hs.FEATURES
    shipped = json.loads(
        (Path(__file__).parent.parent / "configs" / "home_assistant_switches.json").read_text())
    assert shipped["features"]["lutron"] is True, shipped
    print("  OK: lutron feature switch gates both endpoints")


def test_http_and_config_failures_are_reported():
    """A non-2xx response or missing HA config is reported, not raised."""
    print("Testing failure handling...")
    with lutron_env(status_code=401) as post:
        res = lu.light({"light": "kitchen"})
        assert res["status"] == "error", res
        assert "401" in res["message"], res

    with lutron_env() as post:
        with mock.patch.object(ha_api, "load_connection",
                               side_effect=ValueError("Configuration file not found")):
            res = lu.light({"light": "kitchen"})
            assert res["status"] == "error", res
            assert res["error"] == "Lutron call failed", res
            assert "Configuration file not found" in res["message"], res

    # A network error is reported the same way.
    with lutron_env() as post:
        post.side_effect = ha_api.requests.RequestException("connection refused")
        res = lu.light({"light": "kitchen"})
        assert res["status"] == "error", res
        assert "connection refused" in res["message"], res
    print("  OK: HTTP, config, and network failures reported in the result")


def _drain_sos(entity_id, timeout=5.0):
    """Wait for the SOS thread on an entity to finish, so asserts are stable."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        with lu._running_lock:
            if entity_id not in lu._running:
                return True
        _time.sleep(0.01)
    return False


def test_sos_alternates_and_ends_off():
    """The step list alternates every interval and always finishes off."""
    print("Testing the SOS blink pattern...")
    # The default: 10 seconds at 2s -> on off on off on, plus the closing off.
    steps = lu._blink_steps(lu.SOS_DEFAULT_DURATION, lu.SOS_DEFAULT_INTERVAL)
    assert [on for on, _ in steps] == [True, False, True, False, True, False], steps
    assert len(steps) == 6, steps
    assert sum(seconds for _, seconds in steps) == 10.0, steps
    assert steps[-1] == (False, 0.0), steps[-1]        # ends dark, no trailing wait

    # An even number of periods already ends off, so nothing is appended.
    even = lu._blink_steps(8, 2)
    assert [on for on, _ in even] == [True, False, True, False], even
    assert sum(s for _, s in even) == 8.0, even

    # Alternation always starts on, and never has two of the same in a row.
    for duration, interval in ((10, 2), (8, 2), (30, 2), (20, 4), (10, 3), (2, 1)):
        pattern = [on for on, _ in lu._blink_steps(duration, interval)]
        assert pattern[0] is True, (duration, interval, pattern)
        assert pattern[-1] is False, (duration, interval, pattern)
        assert all(a != b for a, b in zip(pattern, pattern[1:])), (duration, pattern)
    print("  OK: 10s default is on/off/on/off/on/off, always ending off")


def test_sos_blinks_in_real_seconds():
    """SOS drives the entity on and off, and leaves it off."""
    print("Testing SOS blinking...")
    with lutron_env() as post, \
            mock.patch.object(lu.time, "sleep"):                # run at full speed
        res = lu.sos({"light": "kitchen"})
        assert res["status"] == "started", res
        assert res["entity_id"] == "light.kitchen_main", res
        assert res["duration"] == 10.0 and res["interval"] == 2.0, res
        assert res["calls"] == 6 and res["estimated_seconds"] == 10.0, res
        assert _drain_sos("light.kitchen_main"), "SOS thread did not finish"

        services = [c.args[0].split("/api/services/")[-1] for c in post.call_args_list]
        assert services == ["light/turn_on", "light/turn_off"] * 3, services
        assert services[-1] == "light/turn_off", "SOS must leave the light off"

        # Dimmers soften an edge, so every step asks for an instant change.
        assert all(c.kwargs["json"].get("transition") == 0 for c in post.call_args_list)
        # Nothing reads or restores the previous state any more.
        assert not any("brightness_pct" in c.kwargs["json"] for c in post.call_args_list)
    print("  OK: 6 calls, alternating, ending off")


def test_sos_honours_duration_and_interval():
    """Longer runs and slower intervals produce the right number of calls."""
    print("Testing SOS duration/interval...")
    with lutron_env() as post, mock.patch.object(lu.time, "sleep"):
        for duration, interval, calls in ((30, 2, 16), (20, 4, 6), (2, 1, 2)):
            post.reset_mock()
            res = lu.sos({"light": "kitchen", "duration": duration, "interval": interval})
            assert res["calls"] == calls, (duration, interval, res)
            assert _drain_sos("light.kitchen_main")
            assert post.call_count == calls, (duration, interval, post.call_count)
            assert post.call_args_list[-1].args[0].endswith("/light/turn_off")
    print("  OK: call count follows duration/interval, always ending off")


def test_sos_on_a_switch_sends_no_transition():
    """A switch.* entity cannot take a transition - it would be a schema error."""
    print("Testing SOS on a switch entity...")
    with lutron_env() as post, mock.patch.object(lu.time, "sleep"):
        lu.sos({"light": "hallway", "duration": 4, "interval": 2})
        assert _drain_sos("switch.hallway_lights")
        assert all(c.args[0].split("/api/services/")[-1].startswith("switch/")
                   for c in post.call_args_list), post.call_args_list[0].args[0]
        assert not any("transition" in c.kwargs["json"] for c in post.call_args_list)
        assert post.call_args_list[-1].args[0].endswith("/switch/turn_off")
    print("  OK: switch entities blink with no transition field")


def test_sos_one_at_a_time_per_entity():
    """A second SOS on the same entity is refused while the first runs."""
    print("Testing the SOS running guard...")
    with lutron_env() as post:
        started = threading.Event()
        release = threading.Event()

        def blocking_sleep(_seconds):
            started.set()
            release.wait(timeout=5)

        with mock.patch.object(lu.time, "sleep", side_effect=blocking_sleep):
            first = lu.sos({"light": "kitchen"})
            assert first["status"] == "started", first
            assert started.wait(timeout=5), "SOS thread never started"

            second = lu.sos({"light": "kitchen"})
            assert second["error"] == "Already running", second

            # A different entity is unaffected.
            other = lu.sos({"light": "light.other_lamp"})
            assert other["status"] == "started", other

            release.set()
            assert _drain_sos("light.kitchen_main")
            assert _drain_sos("light.other_lamp")

        # The guard is empty again, so a later SOS is accepted.
        with mock.patch.object(lu.time, "sleep"):
            assert lu.sos({"light": "kitchen", "duration": 2, "interval": 1})["status"] == "started"
            assert _drain_sos("light.kitchen_main")
    print("  OK: one SOS per entity, other entities unaffected, guard released")


def test_sos_releases_the_guard_when_a_thread_dies():
    """A crashing thread must not leave the entity permanently blocked."""
    print("Testing SOS guard release on a thread crash...")
    with lutron_env(), \
            mock.patch.object(lu, "call_service", side_effect=RuntimeError("boom")), \
            mock.patch.object(lu.time, "sleep"):
        assert lu.sos({"light": "kitchen", "duration": 2, "interval": 1})["status"] == "started"
        assert _drain_sos("light.kitchen_main"), "guard not released after a crash"
        with lu._running_lock:
            assert "light.kitchen_main" not in lu._running
    print("  OK: the guard is released even when the thread raises")


def test_sos_validation():
    """Bad cycles/unit_ms and the usual naming errors are reported, nothing runs."""
    print("Testing SOS validation...")
    with lutron_env() as post:
        assert lu.sos({})["error"] == "Missing light"
        assert lu.sos("nope")["error"] == "Invalid payload format"
        assert lu.sos({"light": "nowhere"})["error"] == "Unknown light"
        assert lu.sos({"light": "movie"})["error"] == "Unknown light"     # a scene alias
        assert lu.sos({"light": "scene.x"})["error"] == "Wrong entity domain"

        for duration in (0, -1, lu.SOS_MAX_DURATION + 1, "loads", None):
            res = lu.sos({"light": "kitchen", "duration": duration})
            assert res["error"] == "Invalid duration", (duration, res)

        for interval in (0, lu.SOS_MAX_INTERVAL + 1, "fast"):
            res = lu.sos({"light": "kitchen", "interval": interval})
            assert res["error"] == "Invalid interval", (interval, res)

        # An interval longer than the run would give a single on with no off.
        res = lu.sos({"light": "kitchen", "duration": 4, "interval": 10})
        assert res["error"] == "Invalid interval", res
        assert "longer than the duration" in res["message"], res

        assert post.call_count == 0, "a rejected SOS reached Home Assistant"
        with lu._running_lock:
            assert not lu._running, lu._running
    print("  OK: bad payloads rejected, nothing started, guard untouched")


def test_sos_feature_switch():
    """The lutron feature switch stops SOS like the other endpoints."""
    print("Testing the SOS feature switch...")
    with lutron_env() as post:
        hs.set_enabled_for(hs.LUTRON, False)
        res = lu.sos({"light": "kitchen"})
        assert res["status"] == "skipped", res
        assert "lutron" in res["message"], res
        assert post.call_count == 0
        with lu._running_lock:
            assert not lu._running
        hs.set_enabled_for(hs.LUTRON, True)
    print("  OK: a disabled lutron feature blinks nothing")


def test_status_reports_every_configured_light():
    """status() pairs each alias with what Home Assistant says about it."""
    print("Testing the lutron status report...")
    with lutron_env() as post:
        res = lu.status({})
        assert res["status"] == "ok", res
        assert res["count"] == 3, res              # kitchen, hallway, bogus
        assert res["on"] == ["kitchen"], res
        assert res["off"] == ["hallway"], res
        assert res["other"] == ["bogus"], res      # aliased to an entity HA lacks

        kitchen = res["lights"]["kitchen"]
        assert kitchen["entity_id"] == "light.kitchen_main", kitchen
        assert kitchen["state"] == "on" and kitchen["brightness"] == "100%", kitchen
        assert kitchen["name"] == "Kitchen Main", kitchen

        # A switch has no brightness attribute at all.
        assert res["lights"]["hallway"]["brightness"] == "-", res["lights"]["hallway"]

        # A stale alias is reported, not silently dropped.
        assert res["lights"]["bogus"]["state"] == "missing", res["lights"]["bogus"]

        # Reading status must never change anything.
        assert post.call_count == 0, "status made a service call"
    print("  OK: state, brightness and name reported per alias")


def test_status_reads_all_states_in_one_request():
    """However many lights are configured, it is one GET."""
    print("Testing the status request count...")
    with lutron_env() as post:
        env_get = lu.get_states
        with mock.patch.object(ha_api.requests, "get",
                               return_value=mock.Mock(status_code=200, text="[]",
                                                      json=lambda: list(STATE_LIST))) as get:
            lu.status({})
            assert get.call_count == 1, get.call_count
            assert get.call_args.args[0].endswith("/api/states"), get.call_args.args[0]
    print("  OK: a single GET /api/states covers every light")


def test_status_message_is_displayable():
    """The report is a count plus one line per state, brightness inline."""
    print("Testing the status text report...")
    wide = {"lutron": {"lights": {"kitchen": "light.kitchen_main",
                                  "dining": "light.dining",
                                  "hallway": "switch.hallway_lights",
                                  "娜の部屋": "light.nas_room"}}}
    states = STATE_LIST + [
        {"entity_id": "light.dining", "state": "on",
         "attributes": {"brightness": 26, "friendly_name": "Dining"}},   # 26/255 = 10%
        {"entity_id": "light.nas_room", "state": "unavailable",
         "attributes": {"friendly_name": "娜の部屋"}},
    ]
    with lutron_env(entities=wide):
        with mock.patch.object(ha_api.requests, "get",
                               return_value=mock.Mock(status_code=200, text="[]",
                                                      json=lambda: states)):
            message = lu.status({})["message"]
    print("\n" + message + "\n")
    lines = message.splitlines()
    assert len(lines) == 5, lines                        # header, rule, on, off, missing
    assert lines[0] == "Lutron lights — 4 lights", lines[0]
    assert set("-") == set(lines[1]), lines[1]
    # Brightness rides inline with each on light.
    assert lines[2] == "On (2): dining (10%), kitchen (100%)", lines[2]
    assert lines[3] == "Off (1): hallway", lines[3]
    assert lines[4] == "Missing/unavailable (1): 娜の部屋", lines[4]
    # The rule spans the widest line, counting 娜 as two columns.
    assert len(lines[1]) == max(display_width(line) for line in lines), lines[1]
    # No per-light rows any more.
    assert "light.kitchen_main" not in message, message
    assert "" not in lines, "no blank line, so no row block"

    # A switch that is on has no brightness, so it carries no parenthesis.
    switch_on = lu.format_lights([
        {"alias": "hallway", "entity_id": "switch.h", "state": "on",
         "brightness": "-", "name": "H"},
    ])
    assert "On (1): hallway" in switch_on, switch_on
    assert "hallway (" not in switch_on, switch_on

    # A healthy report is three lines - no Missing line at all.
    healthy = lu.format_lights([
        {"alias": "kitchen", "entity_id": "light.k", "state": "on",
         "brightness": "100%", "name": "K"},
    ])
    assert len(healthy.splitlines()) == 4, healthy       # header, rule, on, off
    assert "Missing" not in healthy, healthy
    assert healthy.startswith("Lutron lights — 1 light\n"), healthy   # singular
    print("  OK: three lines, brightness inline, no table rows")


def test_status_with_no_aliases_or_no_home_assistant():
    """No aliases reads as empty; an unreachable Home Assistant is an error."""
    print("Testing status edge cases...")
    with lutron_env(entities={"lutron": {"lights": {}}}):
        res = lu.status({})
        assert res["status"] == "ok" and res["count"] == 0, res
        assert res["lights"] == {} and res["on"] == [], res
        assert res["message"] == "Lutron lights — none configured.", res["message"]
        assert lu.format_lights([]) == "Lutron lights — none configured."

    # An unreadable Home Assistant is reported, not raised.
    with lutron_env():
        with mock.patch.object(lu, "get_states", return_value=None):
            res = lu.status({})
            assert res["error"] == "Home Assistant unreachable", res

        with mock.patch.object(lu, "get_states",
                               side_effect=ValueError("Configuration file not found")):
            res = lu.status({})
            assert res["error"] == "Lutron status failed", res
            assert "Configuration file not found" in res["message"], res

    # A non-200 or unparseable body yields None from the reader itself.
    with lutron_env() as post:
        for response in (mock.Mock(status_code=500, text="boom"),
                         mock.Mock(status_code=200, text="nope",
                                   json=mock.Mock(side_effect=ValueError("bad"))),
                         mock.Mock(status_code=200, text="{}", json=lambda: {"not": "a list"})):
            with mock.patch.object(ha_api.requests, "get", return_value=response):
                assert lu.status({})["error"] == "Home Assistant unreachable"
    print("  OK: empty, unreachable, and malformed responses all handled")


def test_status_feature_switch():
    """The lutron feature switch stops the status read too."""
    print("Testing the status feature switch...")
    with lutron_env():
        hs.set_enabled_for(hs.LUTRON, False)
        res = lu.status({})
        assert res["status"] == "skipped", res
        assert "lutron" in res["message"], res
        hs.set_enabled_for(hs.LUTRON, True)
        assert lu.status({})["status"] == "ok"
    print("  OK: a disabled lutron feature reports nothing")


def test_webhooks_are_registered():
    """config.json wires both handlers up as secret-protected webhooks."""
    print("Testing lutron webhook registration...")
    config = json.loads((Path(__file__).parent.parent / "configs" / "config.json").read_text())
    hooks = {h["path"]: h for h in config["webhooks"]}
    for path, function in (("/webhook/lutron/light", "light"),
                           ("/webhook/lutron/scene", "scene"),
                           ("/webhook/lutron/sos", "sos"),
                           ("/webhook/lutron/status", "status")):
        hook = hooks.get(path)
        assert hook, f"{path} not registered in configs/config.json"
        assert hook["module"] == "jobs.home_assistant_lutron", hook
        assert hook["function"] == function, hook
        assert hook["require_secret"] is True, hook
        assert hasattr(lu, function), function

    # The alias maps live in the entities file now, not a lutron-specific one.
    entities = json.loads(
        (Path(__file__).parent.parent / "configs" / "home_assistant_entities.json").read_text())
    assert "lutron" in entities, entities
    assert set(entities["lutron"]) == {"lights", "scenes"}, entities["lutron"]
    assert not (Path(__file__).parent.parent / "configs" / "lutron_config.json").exists()

    # One module means one job switch, named after it.
    switches = json.loads(
        (Path(__file__).parent.parent / "configs" / "job_switches.json").read_text())
    assert switches["jobs"]["home_assistant_lutron"] is True, switches
    print("  OK: /webhook/lutron/light and /scene registered, secret required")


def test_shared_api_caller():
    """call_service() builds the standard Home Assistant service request."""
    print("Testing home_assistant_api.call_service()...")
    with mock.patch.object(ha_api, "load_connection", return_value=FAKE_HA_CONFIG), \
            mock.patch.object(ha_api.requests, "post") as post:
        post.return_value = mock.Mock(status_code=201, text="{}")
        res = ha_api.call_service("light", "turn_on", {"entity_id": "light.x"})

    args, kwargs = post.call_args
    assert args[0] == "http://host:8123/api/services/light/turn_on", args[0]
    assert kwargs["json"] == {"entity_id": "light.x"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["timeout"] == 30
    assert res["status"] == "success" and res["service"] == "light.turn_on", res

    # It requires only the two fields every call needs, not a panel or target.
    assert ha_api.REQUIRED_FIELDS == ("HA_BASE_URL", "HA_API_KEY"), ha_api.REQUIRED_FIELDS
    for missing in ({}, {"HA_BASE_URL": "x"}, {"HA_API_KEY": "y"}):
        with mock.patch.object(ha_api, "CONFIG_FILE") as path:
            with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(missing))):
                try:
                    ha_api.load_connection()
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"expected ValueError for {missing}")
    print("  OK: call_service posts to /api/services/<domain>/<service>")


if __name__ == "__main__":
    test_light_on_off_toggle()
    test_brightness_is_a_percentage()
    test_transition_is_passed_through()
    test_scene_activates_only()
    test_raw_entity_ids_work_without_an_alias()
    test_domain_comes_from_the_entity_id()
    test_wrong_domain_for_the_endpoint()
    test_bad_payloads()
    test_missing_alias_file_still_allows_entity_ids()
    test_lutron_feature_switch()
    test_http_and_config_failures_are_reported()
    test_sos_alternates_and_ends_off()
    test_sos_blinks_in_real_seconds()
    test_sos_honours_duration_and_interval()
    test_sos_on_a_switch_sends_no_transition()
    test_sos_one_at_a_time_per_entity()
    test_sos_releases_the_guard_when_a_thread_dies()
    test_sos_validation()
    test_sos_feature_switch()
    test_status_reports_every_configured_light()
    test_status_reads_all_states_in_one_request()
    test_status_message_is_displayable()
    test_status_with_no_aliases_or_no_home_assistant()
    test_status_feature_switch()
    test_webhooks_are_registered()
    test_shared_api_caller()
    print("\nAll Lutron tests passed!")
