#!/usr/bin/env python3
"""Tests for the presence webhook job (jobs/presence_webhook.py).

The presence file is redirected to a temp dir and the logging engine is mocked,
so nothing is written to the repo:

    python3 tests/test_presence_webhook.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.presence_state as ps
import jobs.presence_webhook as pw


class temp_presence_file:
    """Context manager pointing the presence store at a temp file."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(ps, "STATE_DIR", directory),
            mock.patch.object(ps, "STATE_FILE", directory / "presence.json"),
            mock.patch.object(pw, "write_log"),
        ]
        for patch in self._patches:
            patch.start()
        return ps.STATE_FILE

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()
        return False


def test_read_reports_home_and_away():
    """read() with no id lists everyone, split into home/away."""
    print("Testing presence read (everyone)...")
    with temp_presence_file():
        pw.write({"id": "Alex", "state": "home"})
        pw.write({"id": "Sam", "state": "away"})
        pw.write({"state": "home"})  # no id -> the default person

        res = pw.read({})
        assert res["status"] == "ok", res
        assert res["count"] == 3, res
        assert res["home"] == sorted(["Alex", ps.DEFAULT_PERSON]), res
        assert res["away"] == ["Sam"], res
        assert res["people"]["Sam"]["state"] == ps.STATE_AWAY, res
    print("  OK: read returns home/away lists and the full entries")


def test_read_single_person():
    """read() with an id returns just that person; unknown ones are null."""
    print("Testing presence read (single person)...")
    with temp_presence_file():
        pw.write({"id": "Alex", "state": "away"})

        res = pw.read({"id": "Alex"})
        assert res["id"] == "Alex", res
        assert res["state"] == ps.STATE_AWAY, res
        assert res["presence"]["event"] == pw.MANUAL_EVENT, res

        unknown = pw.read({"id": "nobody"})
        assert unknown["presence"] is None and unknown["state"] is None, unknown
    print("  OK: single-person read, unknown person reported as null")


def test_read_empty_store():
    """read() on a store that does not exist yet is empty, not an error."""
    print("Testing presence read (empty store)...")
    with temp_presence_file():
        res = pw.read({})
        assert res["status"] == "ok" and res["count"] == 0, res
        assert res["home"] == [] and res["away"] == [] and res["people"] == {}, res
        assert res["message"] == "Presence — nobody recorded yet.", res
        assert pw.read(None)["count"] == 0
    print("  OK: empty store reads as zero people")


def test_read_message_is_displayable():
    """Every read carries a formatted, ready-to-display message."""
    print("Testing the formatted presence message...")
    with temp_presence_file():
        pw.write({"id": "Alex", "state": "away", "event": "leaving_home"})
        pw.write({"state": "home", "event": "arriving_home"})

        message = pw.read({})["message"]
        print("\n" + message + "\n")
        lines = message.splitlines()
        assert lines[0] == "Presence — 2 people", lines
        assert set("-") == set(lines[1]), lines          # the rule under the header
        assert lines[2] == f"Home (1): {ps.DEFAULT_PERSON}", lines
        assert lines[3] == "Away (1): Alex", lines
        assert lines[4] == "", lines                     # blank line before the rows
        assert lines[5].startswith("Alex  away  since "), lines
        assert lines[5].endswith("(leaving_home)"), lines
        assert lines[6].startswith(f"{ps.DEFAULT_PERSON}"), lines

        # The rule spans the widest line, counting 娜 as two columns.
        assert len(lines[1]) == max(pw._display_width(l) for l in lines), lines

        # Single-person reads get a one-line message; unknown people say so.
        one = pw.read({"id": "Alex"})["message"]
        assert one.startswith("Alex is away since "), one
        assert pw.read({"id": "nobody"})["message"] == "nobody: no presence recorded yet."

        # One person -> singular header.
        assert pw.format_presence({"Alex": {"state": "home"}}).startswith("Presence — 1 person")
    print("  OK: message renders header, home/away lists, and aligned rows")


def test_message_tolerates_odd_entries():
    """A hand-edited file with missing fields still renders."""
    print("Testing message rendering of odd entries...")
    message = pw.format_presence({"Sam": {}, "Alex": {"state": "elsewhere"}})
    assert "Home (0): -" in message and "Away (0): -" in message, message
    assert "Sam" in message and "unknown" in message, message
    assert "elsewhere" in message, message
    assert pw.format_presence({}, person="Sam") == "Sam: no presence recorded yet."
    print("  OK: missing/unexpected fields render as 'unknown' instead of raising")


def test_write_accepts_state_aliases():
    """home/in/true map to home; away/left/out/not_home/false map to away."""
    print("Testing presence write state aliases...")
    with temp_presence_file():
        for alias in ("home", "IN", "true", True):
            res = pw.write({"id": "Alex", "state": alias})
            assert res["presence"]["state"] == ps.STATE_HOME, (alias, res)
        for alias in ("away", "Left", "out", "not_home", "false", False):
            res = pw.write({"id": "Alex", "state": alias})
            assert res["presence"]["state"] == ps.STATE_AWAY, (alias, res)
    print("  OK: friendly state spellings accepted")


def test_write_persists_and_defaults():
    """write() persists to the file, defaulting id and event."""
    print("Testing presence write persistence...")
    with temp_presence_file() as state_file:
        res = pw.write({"state": "away"})
        assert res["id"] == ps.DEFAULT_PERSON, res
        assert res["presence"]["event"] == pw.MANUAL_EVENT, res

        # A caller-supplied event is kept.
        res = pw.write({"state": "home", "event": "front_door"})
        assert res["presence"]["event"] == "front_door", res

        assert state_file.exists(), state_file
        assert ps.get_state(ps.DEFAULT_PERSON)["state"] == ps.STATE_HOME
    print("  OK: write defaults to 娜 / manual_write and persists")


def test_write_rejects_bad_payloads():
    """A missing/invalid state or a non-object payload is an error dict."""
    print("Testing presence write validation...")
    with temp_presence_file():
        missing = pw.write({})
        assert missing["error"] == "Missing state", missing

        invalid = pw.write({"state": "somewhere"})
        assert invalid["error"] == "Invalid state", invalid

        not_object = pw.write("nope")
        assert not_object["error"] == "Invalid payload format", not_object

        none_state = pw.write({"state": None})
        assert none_state["error"] == "Invalid state", none_state

        # Nothing was written.
        assert pw.read({})["count"] == 0
    print("  OK: bad payloads reported as errors, nothing written")


def test_presence_write_never_touches_the_alarm():
    """Reading/writing presence must not arm or disarm the panel.

    Guards the boundary: this job is bookkeeping only. The alarm is changed by
    /webhook/blink/* and the notify handlers, never from here.
    """
    print("Testing that presence read/write leave the alarm alone...")
    import jobs.home_assistant_arm_disarm as hd

    with temp_presence_file(), \
            mock.patch.object(hd, "set_alarm") as set_alarm, \
            mock.patch.object(hd, "run") as run:
        pw.write({"id": "Alex", "state": "away"})
        pw.write({"state": "home"})
        pw.read({})
        pw.read({"id": "Alex"})
        set_alarm.assert_not_called()
        run.assert_not_called()

    # A future refactor that imports the alarm core into this job fails here.
    assert not hasattr(pw, "set_alarm"), "presence_webhook must not import set_alarm"
    print("  OK: no arm/disarm from the presence webhooks")


def test_webhooks_are_registered():
    """config.json wires both handlers up as secret-protected webhooks."""
    print("Testing presence webhook registration...")
    import json

    config = json.loads((Path(__file__).parent.parent / "configs" / "config.json").read_text())
    hooks = {h["path"]: h for h in config["webhooks"]}
    for path, function in (("/webhook/presence/read", "read"),
                           ("/webhook/presence/write", "write")):
        hook = hooks.get(path)
        assert hook, f"{path} not registered in configs/config.json"
        assert hook["module"] == "jobs.presence_webhook", hook
        assert hook["function"] == function, hook
        assert hook["require_secret"] is True, hook
        assert hasattr(pw, function), function
    print("  OK: /webhook/presence/read and /write registered, secret required")


if __name__ == "__main__":
    test_read_reports_home_and_away()
    test_read_single_person()
    test_read_empty_store()
    test_read_message_is_displayable()
    test_message_tolerates_odd_entries()
    test_write_accepts_state_aliases()
    test_write_persists_and_defaults()
    test_write_rejects_bad_payloads()
    test_presence_write_never_touches_the_alarm()
    test_webhooks_are_registered()
    print("\nAll presence webhook tests passed!")
