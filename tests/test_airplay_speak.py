#!/usr/bin/env python3
"""Tests for the direct-AirPlay text-to-speech job (jobs/airplay_speak.py).

The pyatv stream and the logging engine are mocked and the config is redirected,
so nothing is sent to a real speaker and nothing is written to the repo. The one
exception is `say`, which is exercised for real when available — it is local, free
and silent when writing to a file:

    python3 tests/test_airplay_speak.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.airplay_speak as ap
import jobs.presence_state as ps

CONFIG = {"speakers": {"bedroom": "10.0.0.155", "kitchen": "10.0.0.156"},
          "voice": None, "rate": None}
ONE_SPEAKER = {"speakers": {"bedroom": "10.0.0.155"}, "voice": None, "rate": None}


class airplay_env:
    """Redirects the config, stubs the stream, and captures the log."""

    def __init__(self, config=CONFIG):
        self._config = config

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "airplay_config.json"
        path.write_text(json.dumps(self._config), encoding="utf-8")

        # Records (address, audio_path, volume) per stream, without touching a
        # real speaker. Async, because the real one is awaited.
        self.streamed = []

        async def fake_stream(address, audio, volume):
            self.streamed.append((address, Path(audio), volume))

        self.audio = Path(self._tmp.name) / "audio"
        self._patches = [
            mock.patch.object(ap, "CONFIG_FILE", str(path)),
            mock.patch.object(ap, "AUDIO_DIR", self.audio),
            mock.patch.object(ap, "_stream", fake_stream),
            mock.patch.object(ap, "write_log"),
        ]
        started = [patch.start() for patch in self._patches]
        self.log = started[-1]
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False

    def drain(self, address, timeout=15.0):
        """Wait for the background thread on an address to finish."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with ap._speaking_lock:
                if address not in ap._speaking:
                    return True
            time.sleep(0.01)
        return False

    def log_entry(self):
        """The (log_type, entry) of the most recent write_log call."""
        assert self.log.call_args is not None, "nothing was logged"
        return self.log.call_args.args[0], self.log.call_args.args[1]


def test_speaker_resolution():
    """Aliases, raw addresses, and the single-speaker default all resolve."""
    print("Testing speaker resolution...")
    with airplay_env():
        assert ap.resolve_speaker("bedroom") == "10.0.0.155"
        assert ap.resolve_speaker("kitchen") == "10.0.0.156"
        # An IP address needs no alias.
        assert ap.resolve_speaker("10.0.0.99") == "10.0.0.99"
        assert ap.resolve_speaker("nope") is None
        # Two configured and none named: it must not guess.
        assert ap.resolve_speaker(None) is None

        res = ap.speak({"message": "hi"})
        assert res["error"] == "Missing speaker", res
        assert "bedroom" in res["message"] and "kitchen" in res["message"], res

    with airplay_env(config=ONE_SPEAKER) as env:
        assert ap.resolve_speaker(None) == "10.0.0.155"
        assert ap.speak({"message": "hi"})["status"] == "started"
        assert env.drain("10.0.0.155")

    # No config file at all -> raw addresses still work.
    with airplay_env() as env:
        with mock.patch.object(ap, "CONFIG_FILE", "/nonexistent/airplay_config.json"):
            assert ap.all_speakers() == {}
            assert ap.speak({"message": "hi", "speaker": "10.0.0.155"})["status"] == "started"
            assert env.drain("10.0.0.155")
    print("  OK: alias, raw address, one-speaker default; two is ambiguous")


def test_streams_synthesized_audio_then_cleans_up():
    """The words are rendered to a real WAV, streamed, then deleted."""
    print("Testing synthesis, streaming and cleanup...")
    if not shutil.which("say"):
        print("  SKIP: macOS `say` not available")
        return

    with airplay_env() as env:
        res = ap.speak({"message": "Testing one two three", "speaker": "bedroom",
                        "id": "Alex", "volume": 40})
        assert res["status"] == "started", res
        assert res["speaker"] == "10.0.0.155", res
        assert res["id"] == "Alex" and res["volume"] == 40.0, res
        assert env.drain("10.0.0.155"), "the speak thread did not finish"

        assert len(env.streamed) == 1, env.streamed
        address, audio, volume = env.streamed[0]
        assert address == "10.0.0.155", address
        assert volume == 40.0, volume
        # A real WAV was produced, under audio/, and is KEPT for later listening.
        assert audio.suffix == ".wav", audio
        assert audio.parent == env.audio, audio
        assert audio.exists(), "the recording should be kept, not deleted"
        assert audio.name.startswith(ap.AUDIO_PREFIX), audio.name
        assert "Alex" in audio.name, audio.name        # named after the requester
        assert audio.stat().st_size > 1000, audio.stat().st_size
        # The log names the file, so a line can be traced to its recording.
        assert f"[{audio.name}]" in env.log_entry()[1], env.log_entry()[1]
    print("  OK: real WAV written under audio/, kept, and named in the log")


def test_wav_is_a_real_riff_file():
    """`say` is asked for a format RAOP accepts, not its default AIFF."""
    print("Testing the synthesized audio format...")
    if not shutil.which("say"):
        print("  SKIP: macOS `say` not available")
        return

    # Inside the fixture, so nothing is written to the repo's own audio/.
    with airplay_env() as env:
        audio = ap._synthesize("hello", "tester", None, None)
        assert audio.parent == env.audio, audio
        header = audio.read_bytes()[:12]
        # AIFF would start with FORM....AIFF, which pyatv cannot stream.
        assert header[:4] == b"RIFF", header
        assert header[8:12] == b"WAVE", header
        assert audio.stat().st_size > 1000, audio.stat().st_size

        # A message can never be read as shell syntax: `say` takes an argument list.
        tricky = ap._synthesize("; rm -rf / && echo $HOME `whoami`", "tester", None, None)
        assert tricky.stat().st_size > 1000, "the tricky message did not synthesize"

        # A path-like id cannot escape audio/ either.
        escaped = ap._synthesize("hi", "../../etc/passwd", None, None)
        assert escaped.parent == env.audio, escaped
        assert ".." not in escaped.name, escaped.name
    print("  OK: RIFF/WAVE under audio/, shell metacharacters and paths inert")


def test_logged_to_the_default_log_with_the_requester():
    """Every request lands in the default log, words on their own line."""
    print("Testing the airplay speak log entry...")
    if not shutil.which("say"):
        print("  SKIP: macOS `say` not available")
        return
    import jobs.log_engine as le

    with airplay_env() as env:
        ap.speak({"message": "Dinner is ready", "speaker": "bedroom",
                  "id": "Alex", "volume": 80})
        assert env.drain("10.0.0.155")

        log_type, entry = env.log_entry()
        assert log_type == le.DEFAULT_TYPE == "default", log_type
        head, said = entry.split("\n")
        assert said == '"Dinner is ready"', said
        assert "AIRPLAY SPEAK by Alex" in head, head
        assert "10.0.0.155" in head and "at 80%" in head, head
        assert "-> success" in head, head
        assert head.endswith(".wav]"), head        # the recording is named
        assert "Dinner" not in head, "the message must not also be on the header line"

    # Without an id, attributed to the default person like everywhere else.
    with airplay_env() as env:
        res = ap.speak({"message": "hi", "speaker": "bedroom"})
        assert res["id"] == ps.DEFAULT_PERSON == "娜", res
        assert env.drain("10.0.0.155")
        assert f"AIRPLAY SPEAK by {ps.DEFAULT_PERSON}" in env.log_entry()[1]
    print("  OK: default log, requester on line 1, words on line 2")


def test_failures_are_logged_not_raised():
    """A failing stream or synthesis is recorded and releases the speaker."""
    print("Testing failure handling...")
    with airplay_env() as env:
        async def boom(address, audio, volume):
            raise RuntimeError("no AirPlay device answered")

        with mock.patch.object(ap, "_stream", boom), \
                mock.patch.object(ap, "_synthesize", return_value=Path("/tmp/none.wav")):
            assert ap.speak({"message": "hi", "speaker": "bedroom"})["status"] == "started"
            assert env.drain("10.0.0.155"), "a failing stream did not release the speaker"
            head, said = env.log_entry()[1].split("\n")
            assert "ERROR: no AirPlay device answered" in head, head
            assert said == '"hi"', said          # what was attempted is recorded

    # A synthesis failure is handled the same way.
    with airplay_env() as env:
        with mock.patch.object(ap, "_synthesize", side_effect=RuntimeError("say failed")):
            ap.speak({"message": "hi", "speaker": "bedroom"})
            assert env.drain("10.0.0.155")
            assert "ERROR: say failed" in env.log_entry()[1]

        with ap._speaking_lock:
            assert not ap._speaking, ap._speaking
    print("  OK: stream and synthesis failures logged, speaker always released")


def test_one_stream_per_speaker():
    """RAOP holds an exclusive session, so a second request is refused."""
    print("Testing the per-speaker guard...")
    with airplay_env() as env:
        entered = threading.Event()
        release = threading.Event()

        async def blocking_stream(address, audio, volume):
            entered.set()
            release.wait(timeout=10)

        with mock.patch.object(ap, "_stream", blocking_stream), \
                mock.patch.object(ap, "_synthesize", return_value=Path("/tmp/none.wav")):
            first = ap.speak({"message": "one", "speaker": "bedroom"})
            assert first["status"] == "started", first
            assert entered.wait(timeout=10), "the stream never started"

            second = ap.speak({"message": "two", "speaker": "bedroom"})
            assert second["error"] == "Already speaking", second

            # A different speaker is unaffected.
            other = ap.speak({"message": "three", "speaker": "kitchen"})
            assert other["status"] == "started", other

            release.set()
            assert env.drain("10.0.0.155")
            assert env.drain("10.0.0.156")
    print("  OK: one stream per speaker, others unaffected, guard released")


def test_validation():
    """Bad messages and volumes are reported, and nothing is started."""
    print("Testing validation...")
    with airplay_env():
        for payload in ({}, {"message": ""}, {"message": "   "}, {"message": None}):
            assert ap.speak(payload)["error"] == "Missing message", payload

        assert ap.speak({"text": "via text", "speaker": "bedroom"})["status"] == "started"

        res = ap.speak({"message": "x" * (ap.MAX_MESSAGE_LENGTH + 1), "speaker": "bedroom"})
        assert res["error"] == "Message too long", res

        for value in (0, -1, 101, "loud", None):
            res = ap.speak({"message": "hi", "speaker": "bedroom", "volume": value})
            assert res["error"] == "Invalid volume", (value, res)

        assert ap.speak("nope")["error"] == "Invalid payload format"
        assert ap.speak(None)["error"] == "Invalid payload format"

        assert ap.MAX_MESSAGE_LENGTH == 500
    print("  OK: bad payloads rejected before anything is synthesized")


def test_missing_pyatv_is_explained():
    """Without pyatv the webhook says so rather than failing to import."""
    print("Testing the pyatv dependency check...")
    with airplay_env():
        with mock.patch.object(ap, "pyatv", None), \
                mock.patch.object(ap, "PYATV_IMPORT_ERROR", "No module named 'pyatv'"):
            res = ap.speak({"message": "hi", "speaker": "bedroom"})
            assert res["error"] == "pyatv not installed", res
            assert "pip install pyatv" in res["message"], res
    print("  OK: a missing pyatv is a clear error, not an import crash")


def test_purge_keeps_the_newest():
    """purge() trims audio/ to the N most recent recordings."""
    print("Testing purge...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        # Six recordings, oldest to newest by mtime.
        made = []
        for index in range(6):
            f = env.audio / f"{ap.AUDIO_PREFIX}2026083{index}_000000_000_x{ap.AUDIO_SUFFIX}"
            f.write_bytes(b"RIFF" + bytes(100))
            os.utime(f, (1000 + index, 1000 + index))
            made.append(f)

        assert [f.name for f in ap.all_recordings()] == [f.name for f in reversed(made)]

        res = ap.purge({"records": 2})
        assert res["status"] == "ok", res
        assert res["removed"] == 4 and res["kept"] == 2, res
        assert res["records"] == 2, res
        assert "2 most recent" in res["message"], res["message"]
        survivors = sorted(f.name for f in env.audio.iterdir())
        assert survivors == sorted(f.name for f in made[-2:]), survivors

        # records 0 empties it; the directory itself stays.
        assert ap.purge({"records": 0})["removed"] == 2
        assert env.audio.is_dir() and not list(env.audio.iterdir())

        # An empty directory purges to nothing without complaint.
        res = ap.purge({})
        assert res["removed"] == 0 and res["kept"] == 0, res
        assert res["records"] == ap.DEFAULT_KEEP == 20, res
    print("  OK: keeps the newest N, 0 empties it, default is 20")


def test_purge_only_touches_its_own_files():
    """Anything not matching speak_*.wav is left alone."""
    print("Testing purge safety...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        ours = env.audio / f"{ap.AUDIO_PREFIX}20260830_000000_000_x{ap.AUDIO_SUFFIX}"
        ours.write_bytes(b"RIFF")
        strangers = []
        for name in ("keep_me.txt", "someones_music.wav", "notes.md", "speak_no_suffix"):
            f = env.audio / name
            f.write_text("not ours", encoding="utf-8")
            strangers.append(f)

        res = ap.purge({"records": 0})
        assert res["removed"] == 1, res              # only ours
        assert not ours.exists(), "our recording should have gone"
        for f in strangers:
            assert f.exists(), f"purge deleted {f.name}, which is not ours"
    print("  OK: only speak_*.wav is ever deleted")


def test_purge_validation_and_failures():
    """Bad input is reported; an unremovable file is counted, not fatal."""
    print("Testing purge validation...")
    with airplay_env() as env:
        for value in (-1, "loads", [2], {"a": 1}):
            res = ap.purge({"records": value})
            assert res["error"] == "Invalid records", (value, res)
        # A float truncates like int() does, rather than being rejected.
        assert ap.purge({"records": 2.5})["records"] == 2
        assert ap.purge("nope")["error"] == "Invalid payload format"

        # "keep" works as an alternative key.
        env.audio.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            f = env.audio / f"{ap.AUDIO_PREFIX}2026083{index}_000000_000_x{ap.AUDIO_SUFFIX}"
            f.write_bytes(b"RIFF")
            os.utime(f, (1000 + index, 1000 + index))
        assert ap.purge({"keep": 1})["kept"] == 1

        # One file that will not unlink is reported, and the rest still go.
        for index in range(3):
            f = env.audio / f"{ap.AUDIO_PREFIX}2026084{index}_000000_000_x{ap.AUDIO_SUFFIX}"
            f.write_bytes(b"RIFF")
            os.utime(f, (2000 + index, 2000 + index))

        real_unlink = Path.unlink
        def flaky(self, *args, **kwargs):
            if self.name.endswith("20260840_000000_000_x.wav"):
                raise OSError("device or resource busy")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky):
            res = ap.purge({"records": 0})
        assert res["status"] == "ok", res
        assert "failed" in res and len(res["failed"]) == 1, res
        assert "busy" in res["failed"][0], res["failed"]
        assert "could not be removed" in res["message"], res["message"]
    print("  OK: bad input rejected, a locked file is reported not fatal")


def test_purge_webhook_is_registered():
    """config.json wires purge up alongside the two speak paths."""
    print("Testing purge registration...")
    configs = Path(__file__).parent.parent / "configs"
    hooks = {h["path"]: h for h in json.loads((configs / "config.json").read_text())["webhooks"]}
    hook = hooks.get("/webhook/speak/purge")
    assert hook, "/webhook/speak/purge not registered"
    assert hook["module"] == "jobs.airplay_speak" and hook["function"] == "purge", hook
    assert hook["require_secret"] is True, hook

    # audio/ must be gitignored: it holds generated files.
    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "audio/" in gitignore, "audio/ should be gitignored"
    print("  OK: /webhook/speak/purge registered, audio/ gitignored")


def test_webhook_is_registered():
    """config.json wires the handler up, and it has no Home Assistant switch."""
    print("Testing airplay webhook registration...")
    configs = Path(__file__).parent.parent / "configs"

    hooks = {h["path"]: h for h in json.loads((configs / "config.json").read_text())["webhooks"]}
    hook = hooks.get("/webhook/speak/airplay")
    assert hook, "/webhook/speak/airplay not registered in configs/config.json"
    assert hook["module"] == "jobs.airplay_speak", hook
    assert hook["function"] == "speak" and hook["require_secret"] is True, hook

    # The two speak paths are siblings, one per transport.
    assert "/webhook/speak/ha" in hooks, sorted(hooks)

    switches = json.loads((configs / "job_switches.json").read_text())
    assert switches["jobs"]["airplay_speak"] is True, switches

    # Deliberately NOT a Home Assistant feature: this path never touches HA.
    ha = json.loads((configs / "home_assistant_switches.json").read_text())
    assert "airplay" not in ha["features"], ha["features"]
    assert "airplay_speak" not in ha["features"], ha["features"]

    shipped = json.loads((configs / "airplay_config.json").read_text())
    assert "speakers" in shipped, shipped
    print("  OK: registered, job switch on, no HA feature switch")


if __name__ == "__main__":
    test_speaker_resolution()
    test_streams_synthesized_audio_then_cleans_up()
    test_wav_is_a_real_riff_file()
    test_logged_to_the_default_log_with_the_requester()
    test_failures_are_logged_not_raised()
    test_one_stream_per_speaker()
    test_validation()
    test_missing_pyatv_is_explained()
    test_purge_keeps_the_newest()
    test_purge_only_touches_its_own_files()
    test_purge_validation_and_failures()
    test_purge_webhook_is_registered()
    test_webhook_is_registered()
    print("\nAll AirPlay speak tests passed!")
