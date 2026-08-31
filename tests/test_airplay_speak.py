#!/usr/bin/env python3
"""Tests for the direct-AirPlay text-to-speech job (jobs/airplay_speak.py).

The pyatv stream and the logging engine are mocked and the config is redirected,
so nothing is sent to a real speaker and nothing is written to the repo. The one
exception is `say`, which is exercised for real when available — it is local, free
and silent when writing to a file:

    python3 tests/test_airplay_speak.py
"""

import io
import json
import os
import re
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
        # Wait for any thread still speaking before the patches come off.
        # `_speaking` is module-global, so a thread outliving its fixture would
        # both leak "Already speaking" into the next test and — worse — fall back
        # to the real AUDIO_DIR and the real stream.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            with ap._speaking_lock:
                if not ap._speaking:
                    break
            time.sleep(0.01)
        else:
            raise AssertionError(f"threads still speaking on exit: {ap._speaking}")

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


def test_plays_an_existing_recording():
    """With `file` instead of `message`, an existing recording is played as-is."""
    print("Testing file playback...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        doorbell = env.audio / "doorbell.wav"
        doorbell.write_bytes(b"RIFF" + bytes(2000))

        res = ap.speak({"file": "doorbell.wav", "id": "Alex", "volume": 60, "speaker": "bedroom"})
        assert res["status"] == "started", res
        assert res["played"] == "doorbell.wav", res
        assert "spoken" not in res and "voice" not in res, res   # nothing synthesized
        assert env.drain("10.0.0.155")

        # Exactly the file we asked for reached the stream, at the right volume.
        assert len(env.streamed) == 1, env.streamed
        address, audio, volume = env.streamed[0]
        assert audio.name == "doorbell.wav" and volume == 60.0, env.streamed[0]

        # The log names the file and carries no quoted words - there are none.
        log_type, entry = env.log_entry()
        assert log_type == "default", log_type
        assert entry == ("AIRPLAY PLAY by Alex on 10.0.0.155 at 60% "
                         "-> success [doorbell.wav]"), entry
        assert "\n" not in entry, "a played file should log one line only"

        # A file handed to us was never ours to delete.
        assert doorbell.exists(), "playback must not remove the file"
        assert doorbell.read_bytes()[:4] == b"RIFF", "the file was modified"

        # This job's own output can be replayed too.
        made = env.audio / "speak_20260831_101112_000_script.wav"
        made.write_bytes(b"RIFF" + bytes(2000))
        assert ap.speak({"file": made.name, "speaker": "bedroom"})["played"] == made.name
        assert env.drain("10.0.0.155")
        assert made.exists()
    print("  OK: plays the named file, logs only its name, never deletes it")


def test_file_cannot_escape_the_audio_directory():
    """A path in `file` is reduced to its basename, so it cannot walk out."""
    print("Testing file path safety...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        (env.audio / "inside.wav").write_bytes(b"RIFF" + bytes(2000))

        # Something that exists outside audio/ must not be reachable.
        outside = Path(env.audio).parent / "outside.wav"
        outside.write_bytes(b"RIFF" + bytes(2000))
        res = ap.speak({"file": "../outside.wav", "speaker": "bedroom"})
        assert res["error"] == "Unknown file", res
        assert not env.streamed, "a traversal reached the stream"

        # Nothing whose basename is absent or unplayable gets through.
        for attempt in ("../../etc/passwd", "/etc/passwd", "..", ".", "../outside.wav"):
            res = ap.speak({"file": attempt, "speaker": "bedroom"})
            assert res["error"] == "Unknown file", (attempt, res)
            assert not env.streamed, (attempt, "reached the stream")

        # Only the basename matters, so a path whose *last* component is a real
        # file in audio/ does resolve — to that file, never to the given path.
        for attempt in ("subdir/inside.wav", "~/inside.wav", "/tmp/inside.wav"):
            res = ap.speak({"file": attempt, "speaker": "bedroom"})
            assert res["played"] == "inside.wav", (attempt, res)
            assert env.drain("10.0.0.155")
            assert env.streamed[-1][1].parent == env.audio, env.streamed[-1]

        # resolve_recording never returns anything outside audio/.
        for attempt in ("../outside.wav", "/etc/passwd", "a/b/c/inside.wav"):
            target, error = ap.resolve_recording(attempt)
            assert target is None or target.parent == env.audio, (attempt, target)
    print("  OK: only the basename is used; traversal and absolute paths rejected")


def test_file_validation():
    """A missing, unplayable, or conflicting file request is reported."""
    print("Testing file validation...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        (env.audio / "doorbell.wav").write_bytes(b"RIFF" + bytes(2000))
        (env.audio / "notes.txt").write_text("not audio", encoding="utf-8")

        # Wrong extension: caught here rather than failing inside the stream.
        res = ap.speak({"file": "notes.txt", "speaker": "bedroom"})
        assert res["error"] == "Unknown file", res
        assert "playable" in res["message"], res

        # Absent file: the error lists what is actually there.
        res = ap.speak({"file": "nope.wav", "speaker": "bedroom"})
        assert res["error"] == "Unknown file", res
        assert "doorbell.wav" in res["message"], res

        # Both a message and a file is ambiguous for something played out loud.
        res = ap.speak({"message": "hi", "file": "doorbell.wav", "speaker": "bedroom"})
        assert res["error"] == "Conflicting request", res
        assert "not both" in res["message"], res

        # Neither: the error mentions both options.
        res = ap.speak({})
        assert res["error"] == "Missing message", res
        assert "file" in res["message"], res

        # Every playable suffix is accepted by the resolver.
        for suffix in ap.PLAYABLE_SUFFIXES:
            f = env.audio / f"clip{suffix}"
            f.write_bytes(b"RIFF" + bytes(2000))
            target, error = ap.resolve_recording(f.name)
            assert error is None and target == f, (suffix, error)

        # Case-insensitive suffix.
        upper = env.audio / "CLIP.WAV"
        upper.write_bytes(b"RIFF" + bytes(2000))
        assert ap.resolve_recording("CLIP.WAV")[1] is None

        assert not env.streamed, "a rejected request reached the stream"
    print("  OK: extension, absence, and message+file conflicts all reported")


def test_purge_leaves_hand_placed_files_alone():
    """A doorbell.wav you dropped in audio/ survives a purge."""
    print("Testing purge against hand-placed audio...")
    with airplay_env() as env:
        env.audio.mkdir(parents=True, exist_ok=True)
        doorbell = env.audio / "doorbell.wav"
        doorbell.write_bytes(b"RIFF" + bytes(2000))
        mine = env.audio / f"{ap.AUDIO_PREFIX}20260831_000000_000_x{ap.AUDIO_SUFFIX}"
        mine.write_bytes(b"RIFF" + bytes(2000))

        res = ap.purge({"records": 0})
        assert res["removed"] == 1, res              # only this job's own output
        assert not mine.exists(), "the job's recording should have gone"
        assert doorbell.exists(), "purge deleted a hand-placed file"
        # ...and it is still playable afterwards.
        assert ap.resolve_recording("doorbell.wav")[1] is None
    print("  OK: purge only trims speak_*.wav, so custom clips persist")



WAV_BYTES = b"RIFF" + bytes(4) + b"WAVE" + bytes(3000)


def _client():
    """A Flask test client and the secret header, or ``(None, None)`` to skip."""
    try:
        from app import app as flask_app
    except ImportError as e:
        print(f"  SKIP: {e}")
        return None, None
    secret_path = Path(__file__).parent.parent / "configs" / "webhook_secret.json"
    if not secret_path.exists():
        print("  SKIP: configs/webhook_secret.json not set up")
        return None, None
    secret = json.loads(secret_path.read_text())["WEBHOOK_SECRET"]
    return flask_app.test_client(), {"X-Webhook-Secret": secret}


def _upload(client, headers, **fields):
    """POST a one-file multipart upload, returning the job's result dict."""
    data = {"file": (io.BytesIO(WAV_BYTES), fields.pop("_filename", "clip.wav"))}
    data.update({k: str(v) for k, v in fields.items()})
    response = client.post("/webhook/speak/upload", headers=headers, data=data,
                           content_type="multipart/form-data")
    return response.get_json()["result"]


def test_upload_stores_then_plays():
    """An uploaded file lands in audio/ and is played."""
    print("Testing upload then play...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        res = _upload(client, headers, name="front-door.wav", volume=70, id="Alex")
        assert res["status"] == "ok", res
        assert res["stored_as"] == "front-door.wav", res
        assert res["original"] == "clip.wav", res
        assert res["bytes"] == len(WAV_BYTES), res
        assert res["id"] == "Alex" and res["volume"] == 70.0, res
        assert res["play"]["status"] == "started", res
        assert env.drain("10.0.0.155")

        stored = env.audio / "front-door.wav"
        assert stored.is_file(), "the upload was not stored"
        assert stored.read_bytes() == WAV_BYTES, "the stored bytes differ"

        # It was played, at the volume asked for, from audio/.
        assert len(env.streamed) == 1, env.streamed
        _, audio, volume = env.streamed[0]
        assert audio == stored and volume == 70.0, env.streamed[0]

        # Two log lines: the upload, then the playback naming the file.
        assert stored.exists(), "playing an upload must not delete it"
    print("  OK: stored in audio/, played at the given volume, kept")


def test_upload_naming():
    """`name` decides the filename; without one a timestamp is used."""
    print("Testing upload naming...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        # No name -> a date-and-time stem. Note "/" and ":" cannot appear in a
        # filename, so the separators are "-" and "_".
        res = _upload(client, headers)
        assert env.drain("10.0.0.155")
        stem = Path(res["stored_as"]).stem
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", stem), stem
        assert res["stored_as"].endswith(".wav"), res

        # A name without an extension gets one from the upload.
        res = _upload(client, headers, name="doorbell")
        assert env.drain("10.0.0.155")
        assert res["stored_as"] == "doorbell.wav", res

        # "as" works as an alternative key.
        res = _upload(client, headers, **{"as": "chime.wav"})
        assert env.drain("10.0.0.155")
        assert res["stored_as"] == "chime.wav", res

        # A path in the name is reduced to its basename — it cannot escape audio/.
        res = _upload(client, headers, name="../../etc/evil.wav")
        assert env.drain("10.0.0.155")
        assert res["stored_as"] == "evil.wav", res
        assert (env.audio / "evil.wav").is_file()
        assert not (env.audio.parent.parent / "etc" / "evil.wav").exists()

        # Re-uploading the same name replaces it, and says so.
        res = _upload(client, headers, name="doorbell.wav")
        assert env.drain("10.0.0.155")
        assert res["replaced"] is True, res
    print("  OK: timestamp default, extension inferred, paths stripped")


def test_upload_cannot_use_the_reserved_prefix():
    """An upload may not be named speak_*, the only pattern purge deletes."""
    print("Testing the reserved prefix...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        for name in ("speak_sneaky", "speak_sneaky.wav", "SPEAK_shouty.wav",
                     "../speak_evil.wav"):
            res = _upload(client, headers, name=name)
            assert res["error"] == "Invalid name", (name, res)
            assert "reserved" in res["message"], res
        assert not env.audio.exists() or not list(env.audio.glob("speak_*")), \
            "a reserved-prefix upload was stored"
    print("  OK: speak_* is rejected, so an upload can never be purged")


def test_uploads_survive_purge():
    """Whatever it is named, an upload is never trimmed by purge."""
    print("Testing uploads against purge...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        for name in ("doorbell.wav", "chime.mp3", "front-door.wav"):
            _upload(client, headers, name=name)
            assert env.drain("10.0.0.155")

        # Plus one of the job's own synthesized recordings.
        mine = env.audio / f"{ap.AUDIO_PREFIX}20260831_000000_000_x{ap.AUDIO_SUFFIX}"
        mine.write_bytes(b"RIFF")

        res = ap.purge({"records": 0})
        assert res["removed"] == 1, res            # only the synthesized one
        assert not mine.exists(), "the synthesized recording should have gone"
        survivors = sorted(f.name for f in env.audio.iterdir())
        assert survivors == ["chime.mp3", "doorbell.wav", "front-door.wav"], survivors
    print("  OK: purge removes only synthesized speech; uploads persist")


def test_upload_validation():
    """Bad or missing uploads are reported and nothing is stored."""
    print("Testing upload validation...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        # A non-audio file: the type cannot be guessed, so it is refused.
        res = _upload(client, headers, _filename="notes.txt")
        assert res["error"] == "Invalid name", res
        assert "kind of audio" in res["message"], res

        # No file part at all, with a hint about the Shortcut Form field.
        response = client.post("/webhook/speak/upload", headers=headers,
                               data={"name": "x"}, content_type="multipart/form-data")
        res = response.get_json()["result"]
        assert res["error"] == "no files", res
        assert "File" in res["message"], res
        assert "form_fields" in res["debug"], res

        # Two files at once is ambiguous.
        response = client.post(
            "/webhook/speak/upload", headers=headers,
            data={"a": (io.BytesIO(WAV_BYTES), "one.wav"),
                  "b": (io.BytesIO(WAV_BYTES), "two.wav")},
            content_type="multipart/form-data")
        assert response.get_json()["result"]["error"] == "Too many files"

        res = _upload(client, headers, volume=0)
        assert res["error"] == "Invalid volume", res

        assert not env.audio.exists() or not list(env.audio.iterdir()), \
            "a rejected upload was stored"
    print("  OK: non-audio, missing, multiple and bad volume all rejected")


def test_upload_is_stored_even_if_playback_cannot_start():
    """A busy or unreachable speaker must not cost the upload."""
    print("Testing upload with playback blocked...")
    client, headers = _client()
    if not client:
        return

    with airplay_env(config=ONE_SPEAKER) as env:
        # Hold the speaker so playback cannot start.
        with ap._speaking_lock:
            ap._speaking.add("10.0.0.155")
        try:
            res = _upload(client, headers, name="held.wav")
            assert res["status"] == "ok", res             # the upload succeeded
            assert (env.audio / "held.wav").is_file(), "the file was not stored"
            assert res["play"]["error"] == "Already speaking", res
            assert "held.wav" in res["play"]["message"], res["play"]
        finally:
            with ap._speaking_lock:
                ap._speaking.discard("10.0.0.155")
    print("  OK: stored anyway, with the playback failure reported separately")


def test_upload_webhook_is_registered():
    """config.json wires upload up alongside the other speak paths."""
    print("Testing upload registration...")
    configs = Path(__file__).parent.parent / "configs"
    hooks = {h["path"]: h for h in json.loads((configs / "config.json").read_text())["webhooks"]}
    hook = hooks.get("/webhook/speak/upload")
    assert hook, "/webhook/speak/upload not registered"
    assert hook["module"] == "jobs.airplay_speak" and hook["function"] == "upload", hook
    assert hook["require_secret"] is True, hook
    assert {"/webhook/speak/ha", "/webhook/speak/airplay",
            "/webhook/speak/upload", "/webhook/speak/purge"} <= set(hooks), sorted(hooks)
    print("  OK: /webhook/speak/upload registered, secret required")


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
    test_plays_an_existing_recording()
    test_file_cannot_escape_the_audio_directory()
    test_file_validation()
    test_purge_leaves_hand_placed_files_alone()
    test_upload_stores_then_plays()
    test_upload_naming()
    test_upload_cannot_use_the_reserved_prefix()
    test_uploads_survive_purge()
    test_upload_validation()
    test_upload_is_stored_even_if_playback_cannot_start()
    test_upload_webhook_is_registered()
    test_purge_keeps_the_newest()
    test_purge_only_touches_its_own_files()
    test_purge_validation_and_failures()
    test_purge_webhook_is_registered()
    test_webhook_is_registered()
    print("\nAll AirPlay speak tests passed!")
