#!/usr/bin/env python3
"""Tests for the text-to-speech job (jobs/home_assistant_speak.py).

The HTTP call, the logging engine, and every switch file are mocked or
redirected, so no real Home Assistant request is made and nothing is written to
the repo:

    python3 tests/test_speak.py
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.home_assistant_api as ha_api
import jobs.home_assistant_entities as he
import jobs.home_assistant_speak as sp
import jobs.home_assistant_switches as hs
import jobs.presence_state as ps

FAKE_HA_CONFIG = {"HA_BASE_URL": "http://host:8123", "HA_API_KEY": "test-token"}

ENTITIES = {
    "speak": {
        "tts": "tts.google_en_com",
        "speakers": {
            "homepod": "media_player.homepod_mini",
            "kitchen": "media_player.kitchen_homepod",
        },
    },
}

# One speaker, so `speaker` may be omitted.
ONE_SPEAKER = {
    "speak": {"tts": "tts.google_en_com",
              "speakers": {"homepod": "media_player.homepod_mini"}},
}


class speak_env:
    """Mocks the HA connection/HTTP and redirects the entity and switch files."""

    def __init__(self, entities=ENTITIES, status_code=200):
        self._entities = entities
        self._status = status_code

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        (directory / "home_assistant_entities.json").write_text(
            json.dumps(self._entities), encoding="utf-8")

        self.post = mock.Mock(return_value=mock.Mock(status_code=self._status, text="{}"))
        self._patches = [
            mock.patch.object(he, "CONFIG_FILE",
                              str(directory / "home_assistant_entities.json")),
            mock.patch.object(hs, "SWITCH_FILE", directory / "home_assistant_switches.json"),
            mock.patch.object(ha_api, "load_connection", return_value=FAKE_HA_CONFIG),
            mock.patch.object(ha_api.requests, "post", self.post),
            mock.patch.object(sp, "write_log"),
        ]
        started = [patch.start() for patch in self._patches]
        self.log = started[-1]          # the write_log mock, for log assertions
        return self.post

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False


def env_log_call(module):
    """Return the (log_type, line) of the most recent write_log call."""
    args = module.write_log.call_args
    assert args is not None, "nothing was logged"
    return args.args[0], args.args[1]


def _called(post):
    """Return the (url, json body) of the single request that was made."""
    assert post.call_count == 1, f"expected 1 request, got {post.call_count}"
    args, kwargs = post.call_args
    return args[0], kwargs["json"]


def test_speak_calls_tts_speak():
    """The provider is the target entity and the speaker is a separate field."""
    print("Testing tts.speak...")
    with speak_env() as post:
        res = sp.speak({"message": "Dinner is ready", "speaker": "kitchen"})
        url, body = _called(post)
        assert url == "http://host:8123/api/services/tts/speak", url
        # The shape that is easy to get backwards: provider in entity_id, speaker
        # in media_player_entity_id.
        assert body == {
            "entity_id": "tts.google_en_com",
            "media_player_entity_id": "media_player.kitchen_homepod",
            "message": "Dinner is ready",
        }, body
        assert res["status"] == "success", res
        assert res["speaker"] == "media_player.kitchen_homepod", res
        assert res["tts"] == "tts.google_en_com", res
        assert res["spoken"] == "Dinner is ready", res
    print("  OK: posts to tts/speak with the provider and speaker in the right fields")


def test_speaker_resolution():
    """Aliases, raw entity ids, and the single-speaker default all resolve."""
    print("Testing speaker resolution...")
    with speak_env() as post:
        sp.speak({"message": "hi", "speaker": "homepod"})
        assert _called(post)[1]["media_player_entity_id"] == "media_player.homepod_mini"

        # A raw entity id needs no alias.
        post.reset_mock()
        sp.speak({"message": "hi", "speaker": "media_player.unlisted"})
        assert _called(post)[1]["media_player_entity_id"] == "media_player.unlisted"

        # "media_player" works as a generic alternative key.
        post.reset_mock()
        sp.speak({"message": "hi", "media_player": "kitchen"})
        assert _called(post)[1]["media_player_entity_id"] == "media_player.kitchen_homepod"

        # With two configured and none named, it must not guess.
        post.reset_mock()
        res = sp.speak({"message": "hi"})
        assert res["error"] == "Missing speaker", res
        assert "homepod" in res["message"] and "kitchen" in res["message"], res
        assert post.call_count == 0

    # With exactly one configured, the speaker may be omitted.
    with speak_env(entities=ONE_SPEAKER) as post:
        assert sp.speak({"message": "hi"})["status"] == "success"
        assert _called(post)[1]["media_player_entity_id"] == "media_player.homepod_mini"
    print("  OK: alias, entity id, and the one-speaker default; two is ambiguous")


def test_language_is_optional_and_passed_through():
    """A language reaches the provider; without one the field is absent."""
    print("Testing the language field...")
    with speak_env() as post:
        sp.speak({"message": "おかえりなさい", "speaker": "homepod", "language": "ja"})
        body = _called(post)[1]
        assert body["language"] == "ja", body
        assert body["message"] == "おかえりなさい", body

        post.reset_mock()
        sp.speak({"message": "hello", "speaker": "homepod"})
        assert "language" not in _called(post)[1]
    print("  OK: language forwarded when given, omitted when not")


def test_message_validation():
    """A missing, blank, or overlong message is reported and nothing is spoken."""
    print("Testing message validation...")
    with speak_env() as post:
        for payload in ({}, {"message": ""}, {"message": "   "}, {"message": None}):
            res = sp.speak(payload)
            assert res["error"] == "Missing message", (payload, res)

        # "text" works as an alternative key.
        sp.speak({"text": "via the text key", "speaker": "homepod"})
        assert _called(post)[1]["message"] == "via the text key"

        post.reset_mock()
        long_message = "x" * (sp.MAX_MESSAGE_LENGTH + 1)
        res = sp.speak({"message": long_message, "speaker": "homepod"})
        assert res["error"] == "Message too long", res
        assert str(sp.MAX_MESSAGE_LENGTH) in res["message"], res

        # Exactly at the limit is fine.
        post.reset_mock()
        assert sp.speak({"message": "x" * sp.MAX_MESSAGE_LENGTH,
                         "speaker": "homepod"})["status"] == "success"

        assert sp.speak("nope")["error"] == "Invalid payload format"
        assert sp.speak(None)["error"] == "Invalid payload format"
    print("  OK: blank and overlong messages rejected, 'text' accepted")


def test_volume_is_set_before_speaking():
    """A volume becomes a media_player.volume_set call, ahead of the speech."""
    print("Testing the volume field...")
    with speak_env() as post:
        res = sp.speak({"message": "Loud alert", "speaker": "homepod", "volume": 80})
        assert post.call_count == 2, post.call_args_list
        first, second = post.call_args_list
        # Order matters: the level must be in place before the audio starts.
        assert first.args[0].endswith("/media_player/volume_set"), first.args[0]
        assert second.args[0].endswith("/tts/speak"), second.args[0]
        # Home Assistant takes 0.0-1.0, not a percentage.
        assert first.kwargs["json"] == {
            "entity_id": "media_player.homepod_mini", "volume_level": 0.8,
        }, first.kwargs["json"]
        assert res["volume"] == 80.0, res
        assert "volume_error" not in res, res

        # Percentages convert to three decimals, so a third of the way works.
        post.reset_mock()
        sp.speak({"message": "hi", "speaker": "homepod", "volume": 15})
        assert post.call_args_list[0].kwargs["json"]["volume_level"] == 0.15

        # Without a volume there is exactly one call and nothing is changed.
        post.reset_mock()
        res = sp.speak({"message": "hi", "speaker": "homepod"})
        assert post.call_count == 1, post.call_args_list
        assert post.call_args.args[0].endswith("/tts/speak")
        assert res["volume"] is None, res
    print("  OK: volume_set precedes tts.speak, converted to a 0.0-1.0 level")


def test_volume_validation():
    """A volume outside 1-100, or zero, is rejected and nothing is spoken."""
    print("Testing volume validation...")
    with speak_env() as post:
        # 0 would speak silently while reporting success - a silent no-op.
        res = sp.speak({"message": "hi", "speaker": "homepod", "volume": 0})
        assert res["error"] == "Invalid volume", res
        assert "silently" in res["message"], res

        for value in (-1, 101, 140, "loud", None):
            res = sp.speak({"message": "hi", "speaker": "homepod", "volume": value})
            assert res["error"] == "Invalid volume", (value, res)

        assert post.call_count == 0, "a rejected volume still reached Home Assistant"

        # The boundaries are fine.
        for value in (1, 100, 33.5):
            post.reset_mock()
            assert sp.speak({"message": "hi", "speaker": "homepod",
                             "volume": value})["status"] == "success", value
    print("  OK: 0 and out-of-range rejected, 1-100 accepted, nothing sent")


def test_speech_survives_a_failed_volume_change():
    """If the volume cannot be set, the message still goes out and says so."""
    print("Testing a failed volume change...")
    with speak_env() as post:
        def flaky(url, **kwargs):
            failed = "volume_set" in url
            return mock.Mock(status_code=500 if failed else 200, text="nope")

        post.side_effect = flaky
        res = sp.speak({"message": "still speaks", "speaker": "homepod", "volume": 50})
        # Speaking at the wrong volume beats not speaking at all.
        assert res["status"] == "success", res
        assert res["volume"] == 50.0, res
        assert "volume_error" in res and "500" in res["volume_error"], res
        assert post.call_count == 2, post.call_args_list
        assert post.call_args_list[-1].args[0].endswith("/tts/speak")
    print("  OK: the message is spoken anyway, with volume_error reported")


def test_request_is_logged_to_the_default_log_with_the_requester():
    """Every speak request is recorded in the default log, naming who asked."""
    print("Testing the speak log entry...")
    import jobs.log_engine as le

    with speak_env() as post:
        res = sp.speak({"message": "Dinner is ready", "id": "Alex",
                        "speaker": "homepod", "volume": 80})
        assert res["id"] == "Alex", res

        assert sp.LOG_TYPE == le.DEFAULT_TYPE == "default", sp.LOG_TYPE
        log_type, entry = env_log_call(sp)
        assert log_type == "default", log_type          # not "blink"

        # The spoken words get their own line, so a long message stays readable.
        head, said = entry.split("\n")
        assert said == '"Dinner is ready"', said
        assert "SPEAK by Alex" in head, head
        assert "media_player.homepod_mini" in head, head
        assert "at 80%" in head, head
        assert head.endswith("-> success"), head
        assert "Dinner" not in head, "the message must not also be on the header line"

    # Without an id the request is attributed to the default person, like
    # everywhere else in this server - never to nobody.
    with speak_env():
        res = sp.speak({"message": "hi", "speaker": "homepod"})
        assert res["id"] == ps.DEFAULT_PERSON == "娜", res
        assert f"SPEAK by {ps.DEFAULT_PERSON}" in env_log_call(sp)[1]

    # A blank or non-scalar id falls back too.
    with speak_env():
        for bad in ("", "   ", None, {"a": 1}):
            assert sp.speak({"message": "hi", "speaker": "homepod",
                             "id": bad})["id"] == ps.DEFAULT_PERSON, bad

    # The language shows up, and a failure is recorded rather than hidden.
    with speak_env():
        sp.speak({"message": "おかえりなさい", "speaker": "homepod", "language": "ja"})
        head, said = env_log_call(sp)[1].split("\n")
        assert "[ja]" in head, head
        assert said == '"おかえりなさい"', said

    with speak_env():
        with mock.patch.object(sp, "call_service",
                               side_effect=ValueError("Configuration file not found")):
            sp.speak({"message": "hi", "id": "Sam", "speaker": "homepod"})
            log_type, entry = env_log_call(sp)
            assert log_type == "default", log_type
            head, said = entry.split("\n")
            assert "SPEAK by Sam ERROR" in head, head
            # Even a failure records what was attempted.
            assert said == '"hi"', said
    print("  OK: logged to default.log, requester on line 1, words on line 2")


def test_misconfiguration_is_explained():
    """A missing or wrong-domain provider/speaker says what to fix."""
    print("Testing configuration errors...")
    # No provider at all.
    with speak_env(entities={"speak": {"speakers": {"a": "media_player.a"}}}) as post:
        res = sp.speak({"message": "hi", "speaker": "a"})
        assert res["error"] == "No TTS provider configured", res
        assert "home_assistant_entities.json" in res["message"], res
        assert post.call_count == 0

    # The speaker put in the tts slot - the easy mistake this guards.
    with speak_env(entities={"speak": {"tts": "media_player.homepod_mini",
                                       "speakers": {"a": "media_player.a"}}}) as post:
        res = sp.speak({"message": "hi", "speaker": "a"})
        assert res["error"] == "Wrong TTS entity", res
        assert post.call_count == 0

    # An alias pointing at something that is not a media player.
    with speak_env(entities={"speak": {"tts": "tts.google_en_com",
                                       "speakers": {"oops": "light.kitchen"}}}) as post:
        res = sp.speak({"message": "hi", "speaker": "oops"})
        assert res["error"] == "Wrong speaker domain", res
        assert "media_player" in res["message"], res
        assert post.call_count == 0

    # An unknown alias lists the known ones.
    with speak_env() as post:
        res = sp.speak({"message": "hi", "speaker": "nowhere"})
        assert res["error"] == "Unknown speaker", res
        assert "homepod" in res["message"], res
        assert post.call_count == 0

    # No entities file at all -> raw entity ids still work.
    with speak_env() as post:
        with mock.patch.object(he, "CONFIG_FILE", "/nonexistent/entities.json"):
            assert sp.all_speakers() == {}
            assert sp.tts_entity() is None
            assert sp.speak({"message": "hi",
                             "speaker": "media_player.x"})["error"] == "No TTS provider configured"
    print("  OK: each misconfiguration names the file and field to fix")


def test_failures_are_reported_not_raised():
    """A non-2xx response or missing HA config is reported in the result."""
    print("Testing failure handling...")
    with speak_env(status_code=401):
        res = sp.speak({"message": "hi", "speaker": "homepod"})
        assert res["status"] == "error", res
        assert "401" in res["message"], res

    with speak_env():
        with mock.patch.object(sp, "call_service",
                               side_effect=ValueError("Configuration file not found")):
            res = sp.speak({"message": "hi", "speaker": "homepod"})
            assert res["error"] == "Speak failed", res
            assert "Configuration file not found" in res["message"], res
    print("  OK: HTTP and config failures reported, never raised")


def test_speak_feature_switch():
    """The speak HA feature switch stops it entirely."""
    print("Testing the speak feature switch...")
    with speak_env() as post:
        hs.set_enabled_for(hs.SPEAK, False)
        res = sp.speak({"message": "hi", "speaker": "homepod"})
        assert res["status"] == "skipped", res
        assert "speak" in res["message"], res
        assert post.call_count == 0

        hs.set_enabled_for(hs.SPEAK, True)
        assert sp.speak({"message": "hi", "speaker": "homepod"})["status"] == "success"

    assert hs.SPEAK == "speak"
    assert hs.SPEAK in hs.FEATURES, hs.FEATURES
    # Shipped OFF: Home Assistant cannot currently see the HomePod, so the
    # feature stays disabled until that is fixed. The code and its tests remain,
    # and one POST to /ha/speak/enable brings it back.
    shipped = json.loads(
        (Path(__file__).parent.parent / "configs" / "home_assistant_switches.json").read_text())
    assert shipped["features"]["speak"] is False, shipped
    print("  OK: a disabled speak feature says nothing")


def test_webhook_is_registered():
    """config.json wires the handler up as a secret-protected webhook."""
    print("Testing speak webhook registration...")
    config = json.loads((Path(__file__).parent.parent / "configs" / "config.json").read_text())
    hooks = {h["path"]: h for h in config["webhooks"]}
    hook = hooks.get("/webhook/speak/ha")
    assert hook, "/webhook/speak/ha not registered in configs/config.json"
    assert hook["module"] == "jobs.home_assistant_speak", hook
    assert hook["function"] == "speak", hook
    assert hook["require_secret"] is True, hook
    assert hasattr(sp, "speak")

    switches = json.loads(
        (Path(__file__).parent.parent / "configs" / "job_switches.json").read_text())
    assert switches["jobs"]["home_assistant_speak"] is True, switches

    entities = json.loads(
        (Path(__file__).parent.parent / "configs" / "home_assistant_entities.json").read_text())
    assert "speak" in entities, entities
    assert set(entities["speak"]) == {"tts", "speakers"}, entities["speak"]
    print("  OK: /webhook/speak/ha registered, secret required, config in place")


if __name__ == "__main__":
    test_speak_calls_tts_speak()
    test_speaker_resolution()
    test_language_is_optional_and_passed_through()
    test_message_validation()
    test_volume_is_set_before_speaking()
    test_volume_validation()
    test_speech_survives_a_failed_volume_change()
    test_request_is_logged_to_the_default_log_with_the_requester()
    test_misconfiguration_is_explained()
    test_failures_are_reported_not_raised()
    test_speak_feature_switch()
    test_webhook_is_registered()
    print("\nAll speak tests passed!")
