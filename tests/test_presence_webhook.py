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
        assert res["home_id"] == sorted(["Alex", ps.DEFAULT_PERSON]), res
        assert res["away_id"] == ["Sam"], res
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
        assert res["home_id"] == [] and res["away_id"] == [] and res["people"] == {}, res
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
        assert len(lines[1]) == max(pw.display_width(l) for l in lines), lines

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


def test_home_defaults_and_routes():
    """A post with no "home" lands in home A; "home" routes it elsewhere."""
    print("Testing the home payload key...")
    assert ps.resolve_home({"home": "M"}) == "M"
    assert ps.resolve_home({"home": "  M  "}) == "M"
    assert ps.resolve_home({"home": ""}) == ps.DEFAULT_HOME
    assert ps.resolve_home({"home": None}) == ps.DEFAULT_HOME
    assert ps.resolve_home({"home": {"nested": 1}}) == ps.DEFAULT_HOME
    assert ps.resolve_home({}) == ps.DEFAULT_HOME
    assert ps.resolve_home(None) == ps.DEFAULT_HOME
    assert ps.DEFAULT_HOME == "A"

    with temp_presence_file():
        # No "home" -> home A, and reading home A back finds it.
        assert pw.write({"id": "Alex", "state": "home"})["home"] == "A"
        assert pw.read({})["home_id"] == ["Alex"]
        assert pw.read({"home": "A"})["home_id"] == ["Alex"]

        # A named home is a separate namespace.
        assert pw.write({"id": "Sam", "state": "home", "home": "M"})["home"] == "M"
        assert pw.read({"home": "M"})["home_id"] == ["Sam"]
        assert pw.read({})["home_id"] == ["Alex"], "home M leaked into home A"
    print("  OK: home defaults to A, a named home routes separately")


def test_same_person_is_independent_per_home():
    """The same id in two homes is two entries that do not affect each other."""
    print("Testing per-home isolation of one person...")
    with temp_presence_file():
        pw.write({"id": "Sam", "state": "home", "home": "A"})
        pw.write({"id": "Sam", "state": "away", "home": "M"})

        assert pw.read({"id": "Sam", "home": "A"})["state"] == ps.STATE_HOME
        assert pw.read({"id": "Sam", "home": "M"})["state"] == ps.STATE_AWAY
        assert ps.anyone_home(home="A") is True
        assert ps.anyone_home(home="M") is False

        # Changing one home leaves the other alone.
        pw.write({"id": "Sam", "state": "away", "home": "A"})
        assert pw.read({"id": "Sam", "home": "A"})["state"] == ps.STATE_AWAY
        assert pw.read({"id": "Sam", "home": "M"})["state"] == ps.STATE_AWAY
    print("  OK: one id in two homes stays two independent entries")


def test_read_all_homes():
    """home="all" returns every home, with a total count and a tagged message."""
    print("Testing the all-homes read...")
    with temp_presence_file():
        pw.write({"id": "Alex", "state": "away", "home": "A"})
        pw.write({"state": "home", "home": "A"})       # no id -> 娜
        pw.write({"id": "Sam", "state": "home", "home": "M"})

        res = pw.read({"home": "all"})
        assert res["status"] == "ok" and res["home"] == "all", res
        assert res["count"] == 3, res
        assert set(res["homes"]) == {"A", "M"}, res
        assert res["homes"]["A"]["count"] == 2, res
        assert res["homes"]["A"]["home_id"] == [ps.DEFAULT_PERSON], res
        assert res["homes"]["A"]["away_id"] == ["Alex"], res
        assert res["homes"]["M"]["home_id"] == ["Sam"], res

        message = res["message"]
        print("\n" + message + "\n")
        lines = message.splitlines()
        assert lines[0] == "Presence — 3 people across 2 homes", lines
        assert set("-") == set(lines[1]), lines
        assert lines[2].startswith("[A] Home (1)"), lines
        assert lines[3].startswith("[M] Home (1)"), lines
        assert any(l.startswith("[M] Sam") for l in lines), lines
        # The rule spans the widest line, counting 娜 as two columns.
        assert len(lines[1]) == max(pw.display_width(l) for l in lines), lines

        # "all" is matched case-insensitively.
        assert pw.read({"home": "ALL"})["count"] == 3
    print("  OK: all-homes read totals every house and tags each line")


def test_read_unknown_and_empty_homes():
    """An unknown home reads as empty rather than as an error."""
    print("Testing reads of unknown/empty homes...")
    with temp_presence_file():
        res = pw.read({"home": "nowhere"})
        assert res["status"] == "ok" and res["count"] == 0, res
        assert res["home"] == "nowhere", res
        assert res["home_id"] == [] and res["away_id"] == [], res
        assert res["message"] == "Presence — nobody recorded yet.", res

        # An unknown person inside an unknown home is null, not an error.
        one = pw.read({"home": "nowhere", "id": "ghost"})
        assert one["presence"] is None and one["state"] is None, one

        # No homes at all -> the all-homes view is empty too.
        assert pw.read({"home": "all"})["count"] == 0
        assert pw.read({"home": "all"})["message"] == "Presence — nobody recorded yet."
        assert pw.format_all_homes({}) == "Presence — nobody recorded yet."
        assert pw.format_all_homes({"A": {}}) == "Presence — nobody recorded yet."
    print("  OK: unknown and empty homes read as empty, not an error")


def test_write_rejects_the_reserved_home_name():
    """"all" is how a read asks for every home, so it cannot be written to."""
    print("Testing the reserved home name...")
    with temp_presence_file():
        res = pw.write({"id": "Sam", "state": "home", "home": "all"})
        assert res["error"] == "Reserved home name", res
        assert pw.write({"state": "home", "home": "ALL"})["error"] == "Reserved home name"
        # Nothing was written anywhere.
        assert pw.read({"home": "all"})["count"] == 0
    print("  OK: writing to home 'all' is rejected, nothing stored")


def test_legacy_file_migrates_into_home_a():
    """A pre-multi-home presence.json is read as home A and rewritten nested."""
    print("Testing migration of a single-home presence file...")
    import json

    with temp_presence_file() as state_file:
        state_file.write_text(json.dumps({
            "people": {
                "娜": {"state": "away", "event": "leaving_home", "last_updated": "2026-08-18 22:04:21.680"},
                "Alex": {"state": "home", "event": "arriving_home", "last_updated": "2026-08-18 22:04:21.678"},
            },
            "last_modified": "2026-08-18 22:04:21.680",
        }, ensure_ascii=False), encoding="utf-8")

        # Read as home A, with both entries intact.
        res = pw.read({})
        assert res["home"] == "A" and res["count"] == 2, res
        assert res["home_id"] == ["Alex"] and res["away_id"] == [ps.DEFAULT_PERSON], res
        assert ps.get_state("Alex")["event"] == "arriving_home", res

        # The next write persists the nested shape, keeping the old entries.
        pw.write({"id": "Sam", "state": "home", "home": "M"})
        on_disk = json.loads(state_file.read_text(encoding="utf-8"))
        assert "people" not in on_disk, on_disk
        assert set(on_disk["homes"]) == {"A", "M"}, on_disk
        assert set(on_disk["homes"]["A"]["people"]) == {"娜", "Alex"}, on_disk
        assert on_disk["homes"]["M"]["people"]["Sam"]["state"] == ps.STATE_HOME, on_disk
    print("  OK: legacy file migrated into home A, entries preserved")


def test_store_survives_odd_home_shapes():
    """A hand-edited file with unexpected home shapes reads as empty, not fatal."""
    print("Testing recovery from odd home shapes...")
    import json

    with temp_presence_file() as state_file:
        for bad in ('{"homes": "nope"}', '{"homes": {"A": "nope"}}',
                    '{"homes": {"A": {"people": "nope"}}}', '[]'):
            state_file.write_text(bad, encoding="utf-8")
            assert pw.read({})["count"] == 0, bad
            assert pw.read({"home": "all"})["count"] == 0, bad

        # A write on top of a broken file still works.
        ps.set_state("Alex", ps.STATE_HOME, home="A")
        assert ps.get_state("Alex", home="A")["state"] == ps.STATE_HOME
    print("  OK: odd home shapes read as empty and are recoverable")


def _client_and_headers():
    """A Flask test client and the secret header, or ``(None, None)`` to skip.

    The HTTP tests need Flask installed and configs/webhook_secret.json set up;
    without either there is nothing to test, so they say so and pass.
    """
    import json

    try:
        from app import app as flask_app
    except ImportError as e:  # Flask not installed (see requirements.txt)
        print(f"  SKIP: {e}")
        return None, None

    secret_path = Path(__file__).parent.parent / "configs" / "webhook_secret.json"
    if not secret_path.exists():
        print("  SKIP: configs/webhook_secret.json not set up")
        return None, None

    secret = json.loads(secret_path.read_text())["WEBHOOK_SECRET"]
    return flask_app.test_client(), {"X-Webhook-Secret": secret}


def test_get_presence_accepts_home():
    """GET /presence?home= selects a house; it composes with ?id= and ?format=."""
    print("Testing GET /presence?home=...")
    client, headers = _client_and_headers()
    if not client:
        return

    with temp_presence_file():
        pw.write({"id": "Alex", "state": "away", "home": "A"})
        pw.write({"id": "Sam", "state": "home", "home": "M"})

        # No ?home= -> home A, exactly as before.
        body = client.get("/presence", headers=headers).get_data(as_text=True)
        assert "Alex" in body and "Sam" not in body, body

        # ?home= selects the other house.
        body = client.get("/presence?home=M", headers=headers).get_data(as_text=True)
        assert "Sam" in body and "Alex" not in body, body

        # ?home=all shows both, tagged.
        body = client.get("/presence?home=all", headers=headers).get_data(as_text=True)
        assert "[A] Alex" in body and "[M] Sam" in body, body

        # ?format=json composes with ?home=.
        data = client.get("/presence?home=M&format=json", headers=headers).get_json()
        assert data["home"] == "M" and data["home_id"] == ["Sam"], data

        # ?id= composes with ?home=.
        data = client.get("/presence?home=M&id=Sam&format=json", headers=headers).get_json()
        assert data["id"] == "Sam" and data["state"] == ps.STATE_HOME, data

        # The same id in the other house is a different entry.
        data = client.get("/presence?home=A&id=Sam&format=json", headers=headers).get_json()
        assert data["presence"] is None, data
    print("  OK: ?home= selects a house and composes with ?id= and ?format=json")


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
    test_home_defaults_and_routes()
    test_same_person_is_independent_per_home()
    test_read_all_homes()
    test_read_unknown_and_empty_homes()
    test_write_rejects_the_reserved_home_name()
    test_legacy_file_migrates_into_home_a()
    test_store_survives_odd_home_shapes()
    test_get_presence_accepts_home()
    test_presence_write_never_touches_the_alarm()
    test_webhooks_are_registered()
    print("\nAll presence webhook tests passed!")
