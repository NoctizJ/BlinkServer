#!/usr/bin/env python3
"""Tests for the location log job (jobs/location_webhook.py).

Covers its four handlers — log, fetch, history and purge — the store they share
(jobs/location_state.py), and the per-person phone notification
(jobs/location_notify.py). The per-id location files are redirected to a
temp dir, so nothing is written to the repo's state/ folder:

    python3 tests/test_location.py
"""

import datetime
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.location_notify as ln
import jobs.location_state as ls
import jobs.location_webhook as lw
import jobs.presence_state as ps

APPLE_PARK = {"latitude": 37.334606, "longitude": -122.009102, "address": "Apple Park"}


class temp_location_dir:
    """Context manager pointing the location store at a temp directory.

    Yields the temp ``state/`` directory. The notification switches get their own
    ``configs/`` directory beside it and the phone notification itself is mocked,
    so a test never writes to the repo, never reaches Home Assistant, and sees
    only location files in the directory it is given.
    """

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state = root / "state"
        state.mkdir()
        self._patches = [
            mock.patch.object(ls, "STATE_DIR", state),
            mock.patch.object(ln, "SWITCH_FILE",
                              root / "configs" / "notify_switches.json"),
            mock.patch.object(ln, "notify_phone",
                              return_value={"status": "success", "message": "sent"}),
        ]
        self.notify = [patch.start() for patch in self._patches][-1]
        return state

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()
        return False


def _client_and_headers():
    """A Flask test client and the secret header, or ``(None, None)`` to skip.

    The HTTP tests need Flask installed and configs/webhook_secret.json set up;
    without either there is nothing to test, so they say so and pass.
    """
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


def _log_at(person, recorded_at, **fields):
    """Write one entry with a chosen recorded_at, for ageing tests."""
    payload = {"id": person, "latitude": 1.0, "longitude": 2.0, **fields}
    entry = lw.log(payload)["location"]
    store = ls.load_locations(person)
    store["entries"][-1]["recorded_at"] = recorded_at
    ls.save_locations(person, store)
    return entry


def test_every_handler_defaults_the_id():
    """Every location handler falls back to the presence default person."""
    print("Testing the default id across all four handlers...")
    with temp_location_dir():
        # A missing, blank, and non-string id all resolve the same way.
        for payload in ({}, {"id": ""}, {"id": "  "}, {"id": None}):
            logged = lw.log({**payload, "latitude": 1, "longitude": 2})
            assert logged["id"] == ps.DEFAULT_PERSON, (payload, logged)
            assert lw.fetch(payload)["id"] == ps.DEFAULT_PERSON, payload
            assert lw.history(payload)["id"] == ps.DEFAULT_PERSON, payload
            assert lw.purge(payload)["id"] == ps.DEFAULT_PERSON, payload

        # The file is the default person's, and nothing else was created.
        assert ls.latest_location(ps.DEFAULT_PERSON) is not None
        for handler in (lw.fetch, lw.history, lw.purge):
            assert handler(None)["id"] == ps.DEFAULT_PERSON, handler.__name__
    print(f"  OK: all four handlers default the id to {ps.DEFAULT_PERSON}")


def test_history_formats_all_entries_as_text():
    """history renders the whole history as an aligned text table."""
    print("Testing the formatted location history...")
    with temp_location_dir():
        lw.log({"id": "Alex", "latitude": 37.331800, "longitude": -122.031200,
                "time": "2026-08-16 08:07:53.119"})
        lw.log({"id": "Alex", "latitude": 51.5014, "longitude": -0.1419,
                "address": "Buckingham Palace", "time": "2026-08-17 20:04:55.545"})
        lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})

        res = lw.history({"id": "Alex"})
        assert res["status"] == "ok" and res["count"] == 3 and res["total"] == 3, res

        message = res["message"]
        print("\n" + message + "\n")
        lines = message.splitlines()
        assert lines[0] == "Location history — Alex — 3 entries (newest first)", lines
        assert set("-") == set(lines[1]), lines            # the rule under the header
        assert len(lines) == 5, lines                      # header + rule + 3 rows

        # Newest first, six decimal places, right-aligned coordinates.
        assert lines[2].startswith("2026-08-18 09:15:23.123"), lines[2]
        assert " 37.334606, -122.009102" in lines[2], lines[2]
        assert lines[2].endswith("Apple Park"), lines[2]
        assert lines[3].startswith("2026-08-17 20:04:55.545"), lines[3]
        assert " 51.501400,   -0.141900" in lines[3], lines[3]  # padded to align
        assert lines[4].endswith(lw.NO_ADDRESS), lines[4]  # no address -> "-"

        # The coordinate columns line up across every row.
        column = lines[2].index("37.334606")
        assert all(line[column - 1] == " " for line in lines[2:]), lines
        # The rule spans the widest line.
        assert len(lines[1]) == max(lw.display_width(l) for l in lines), lines
    print("  OK: history rendered newest-first with aligned columns")


def test_history_edge_cases():
    """Empty history, a capped read, singular wording, odd entries."""
    print("Testing location history edge cases...")
    with temp_location_dir():
        empty = lw.history({"id": "nobody"})
        assert empty["count"] == 0 and empty["total"] == 0, empty
        assert empty["message"] == "Location history — nobody — nothing logged yet.", empty

        lw.log({"id": "Alex", **APPLE_PARK})
        one = lw.history({"id": "Alex"})["message"]
        assert "— 1 entry (newest first)" in one, one       # singular

        for i in range(4):
            lw.log({"id": "Alex", "latitude": i, "longitude": i})
        capped = lw.history({"id": "Alex", "n": "2"})   # query strings are text
        assert capped["count"] == 2 and capped["total"] == 5, capped
        assert "— 2 of 5 entries" in capped["message"], capped["message"]
        assert len(capped["message"].splitlines()) == 4, capped["message"]

        # A full read does not say "5 of 5"; bad/zero n means everything.
        assert "— 5 entries" in lw.history({"id": "Alex"})["message"]
        for bad in ("0", -1, "lots", True, None):
            assert lw.history({"id": "Alex", "n": bad})["count"] == 5, bad

        # Hand-edited entries render instead of raising.
        ls.save_locations("Sam", {"entries": [{}, {"address": "娜's place", "time": None}]})
        odd = lw.history({"id": "Sam"})["message"]
        print("\n" + odd + "\n")
        assert odd.count(lw.MISSING_COORDINATE) == 4, odd  # 2 rows x lat+lon
        assert "unknown" in odd and "娜's place" in odd, odd
    print("  OK: empty, capped, singular and odd histories all render")


def test_trigger_is_stored_and_shown_everywhere():
    """An optional trigger reaches the store, both readers and the notification."""
    print("Testing the trigger reason...")
    with temp_location_dir():
        res = lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123",
                      "trigger": "arrived home"})
        assert res["location"]["trigger"] == "arrived home", res
        assert res["message"] == (
            "Logged Alex at 37.334606,-122.009102 (Apple Park) "
            "at 2026-08-18 09:15:23.123; trigger: arrived home"), res["message"]

        # Stored, so it survives a reload.
        assert ls.latest_location("Alex")["trigger"] == "arrived home"

        # fetch returns it and mentions it.
        got = lw.fetch({"id": "Alex"})
        assert got["trigger"] == "arrived home", got
        assert got["message"] == (
            "Alex was at Apple Park at 2026-08-18 09:15:23.123 (arrived home)"), got["message"]

        # The notification uses the with-trigger template.
        title, message = ln.notify_phone.call_args[0]
        assert message == (
            "Alex is at Apple Park (2026-08-18 09:15:23.123) — arrived home."), message

        # The history table shows it in brackets after the address.
        lw.log({"id": "Alex", "latitude": 1, "longitude": 2, "reason": "periodic"})  # alias
        table = lw.history({"id": "Alex"})["message"]
        print("\n" + table + "\n")
        assert "[periodic]" in table and "[arrived home]" in table, table
        assert ls.latest_location("Alex")["trigger"] == "periodic"
    print("  OK: trigger stored, in both messages, the notification and the table")


def test_trigger_is_optional_everywhere():
    """Without a trigger nothing gains a dangling clause or a stray bracket."""
    print("Testing a position with no trigger...")
    with temp_location_dir():
        res = lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})
        assert res["location"]["trigger"] is None, res
        assert res["message"].endswith("at 2026-08-18 09:15:23.123"), res["message"]
        assert "trigger" not in res["message"], res["message"]

        got = lw.fetch({"id": "Alex"})
        assert got["trigger"] is None, got
        assert got["message"] == "Alex was at Apple Park at 2026-08-18 09:15:23.123", got

        # The plain template, with no trailing dash.
        assert ln.notify_phone.call_args[0][1] == (
            "Alex is at Apple Park (2026-08-18 09:15:23.123)."), ln.notify_phone.call_args
        assert "[" not in lw.history({"id": "Alex"})["message"]

        # Blank triggers count as absent, and so do entries predating the field.
        for blank in ("", "   ", None):
            assert lw.log({"id": "Sam", "latitude": 1, "longitude": 2,
                           "trigger": blank})["location"]["trigger"] is None, blank
        ls.save_locations("Kim", {"entries": [{"latitude": 1, "longitude": 2,
                                              "time": "2026-08-18 09:15:23.123"}]})
        assert lw.fetch({"id": "Kim"})["trigger"] is None
        assert "[" not in lw.history({"id": "Kim"})["message"]
    print("  OK: absent/blank triggers leave every surface clean")


def test_trigger_notification_templates_are_configurable():
    """message_with_trigger can be overridden per request and in the config."""
    print("Testing the trigger notification templates...")
    configured = {"message": "{id} at {address}",
                  "message_with_trigger": "{id} at {address} because {trigger}"}
    with temp_location_dir(), mock.patch.object(ln, "load_event_text", return_value=configured):
        lw.log({"id": "Alex", **APPLE_PARK, "trigger": "arrived home"})
        assert ln.notify_phone.call_args[0][1] == "Alex at Apple Park because arrived home"

        lw.log({"id": "Alex", **APPLE_PARK})           # no trigger -> plain template
        assert ln.notify_phone.call_args[0][1] == "Alex at Apple Park"

        # A request may override it, and {trigger} works in any template.
        lw.log({"id": "Alex", **APPLE_PARK, "trigger": "manual",
                "message_with_trigger": "why: {trigger} / where: {address}"})
        assert ln.notify_phone.call_args[0][1] == "why: manual / where: Apple Park"

    # With only `message` configured, a triggered position falls back to it.
    with temp_location_dir(), mock.patch.object(ln, "load_event_text",
                                                return_value={"message": "{id}: {trigger}"}):
        lw.log({"id": "Alex", **APPLE_PARK, "trigger": "arrived home"})
        assert ln.notify_phone.call_args[0][1] == "Alex: arrived home"
    print("  OK: with-trigger template configurable, falls back to message")


def test_webhook_accepts_query_params_and_a_headerless_body():
    """A webhook's inputs work as query params, and without a JSON header.

    Regression: `?records=3` used to be dropped, as was a JSON body sent without
    `Content-Type: application/json` — so `purge` silently ran with its defaults,
    against the default person rather than the requested id.
    """
    print("Testing webhook payload sources...")
    client, headers = _client_and_headers()
    if not client:
        return

    def fifteen_for(person):
        ls.save_locations(person, {"entries": []})
        for i in range(15):
            lw.log({"id": person, "latitude": i, "longitude": i})

    with temp_location_dir():
        # 1. JSON body with the header (already worked).
        fifteen_for("Alex")
        res = client.post("/webhook/location/purge", headers=headers,
                          json={"id": "Alex", "records": 3}).get_json()["result"]
        assert (res["id"], res["records"], res["kept"]) == ("Alex", 3, 3), res

        # 2. Query parameters.
        fifteen_for("Alex")
        res = client.post("/webhook/location/purge?id=Alex&records=3",
                          headers=headers).get_json()["result"]
        assert (res["id"], res["records"], res["kept"]) == ("Alex", 3, 3), res

        # 3. A JSON body with no Content-Type header.
        fifteen_for("Alex")
        res = client.post("/webhook/location/purge", headers=headers,
                          data='{"id": "Alex", "records": 3}').get_json()["result"]
        assert (res["id"], res["records"], res["kept"]) == ("Alex", 3, 3), res

        # 4. The body wins over the query string.
        fifteen_for("Alex")
        res = client.post("/webhook/location/purge?records=9", headers=headers,
                          json={"id": "Alex", "records": 3}).get_json()["result"]
        assert (res["records"], res["kept"]) == (3, 3), res

        # 5. Query params reach the other handlers too.
        res = client.post("/webhook/location/log?id=Alex&latitude=1.5&longitude=2.5",
                          headers=headers).get_json()["result"]
        assert res["location"]["latitude"] == 1.5, res
        assert client.post("/webhook/location/history?id=Alex",
                           headers=headers).get_json()["result"]["id"] == "Alex"

        # 6. A non-dict JSON body is ignored rather than crashing the handler.
        assert client.post("/webhook/location/purge", headers=headers,
                           json=["nope"]).get_json()["result"]["id"] == ps.DEFAULT_PERSON
    print("  OK: query params, headerless bodies, body-wins precedence")


def test_purge_keeps_the_most_recent_records_by_default():
    """With no input, purge keeps the DEFAULT_RECORDS newest entries."""
    print("Testing purge by record count...")
    with temp_location_dir():
        for i in range(15):
            lw.log({"id": "Alex", "latitude": i, "longitude": i, "address": f"stop {i}"})

        res = lw.purge({"id": "Alex"})                 # no records, no days
        assert res["status"] == "ok", res
        assert res["mode"] == "records", res
        assert res["records"] == lw.DEFAULT_RECORDS == 10, res
        assert res["days"] is None and res["cutoff"] is None, res
        assert res["removed"] == 5 and res["kept"] == 10, res
        assert res["message"] == (
            "Purged 5 entries beyond the 10 most recent for Alex; 10 kept."), res["message"]

        # The 10 kept are the newest ones, oldest-first as always.
        kept = [e["address"] for e in ls.location_entries("Alex")]
        assert kept == [f"stop {i}" for i in range(5, 15)], kept

        # An explicit count, and one that removes nothing.
        assert lw.purge({"id": "Alex", "records": "3"})["removed"] == 7
        assert [e["address"] for e in ls.location_entries("Alex")] == [
            "stop 12", "stop 13", "stop 14"]
        assert lw.purge({"id": "Alex", "records": 99})["removed"] == 0
        assert lw.purge({"id": "Alex", "keep": 3})["removed"] == 0      # alias

        # records=0 empties the history; the file itself stays.
        zeroed = lw.purge({"id": "Alex", "records": 0})
        assert zeroed["removed"] == 3 and zeroed["kept"] == 0, zeroed
        assert ls.location_entries("Alex") == []
        assert lw.fetch({"id": "Alex"})["found"] is False
        assert ls.location_file("Alex").exists()
    print("  OK: keeps the N newest, default 10, positionally")


def test_purge_records_wins_over_days():
    """When both are given, only records is applied — days is echoed, not used."""
    print("Testing purge with both records and days...")
    with temp_location_dir():
        now = datetime.datetime.now()
        stamp = lambda days_ago: (now - datetime.timedelta(days=days_ago)).strftime(
            ls.TIMESTAMP_FORMAT)[:-3]
        for days_ago in (40, 30, 20, 1):
            _log_at("Alex", stamp(days_ago), address=f"{days_ago} days ago")

        # By days alone, 3 of these are older than 10 days. By records=2, only
        # the 2 oldest go — so the result proves which rule was applied.
        res = lw.purge({"id": "Alex", "records": 2, "days": 10})
        assert res["mode"] == "records", res
        assert res["records"] == 2 and res["days"] == 10.0, res   # both echoed
        assert res["cutoff"] is None, res                        # days not applied
        assert res["removed"] == 2 and res["kept"] == 2, res
        assert res["message"].endswith("(days ignored)"), res["message"]
        assert [e["address"] for e in ls.location_entries("Alex")] == [
            "20 days ago", "1 days ago"]
    print("  OK: records wins, days reported but ignored")


def test_purge_by_days_still_works():
    """Asking for days alone ages entries as before."""
    print("Testing purge by age...")
    with temp_location_dir():
        now = datetime.datetime.now()
        stamp = lambda days_ago: (now - datetime.timedelta(days=days_ago)).strftime(
            ls.TIMESTAMP_FORMAT)[:-3]

        for days_ago in (30, 20, 9, 1):
            _log_at("Alex", stamp(days_ago), address=f"{days_ago} days ago")

        res = lw.purge({"id": "Alex", "days": 10})
        assert res["mode"] == "days", res
        assert res["days"] == 10.0 and res["records"] is None, res
        assert res["cutoff"], res
        assert res["removed"] == 2 and res["kept"] == 2, res
        assert res["undated"] == 0, res
        assert res["message"] == (
            "Purged 2 entries older than 10 days for Alex; 2 kept."), res["message"]

        addresses = [e["address"] for e in ls.location_entries("Alex")]
        assert addresses == ["9 days ago", "1 days ago"], addresses

        # A tighter window takes another one; a wider one takes nothing.
        assert lw.purge({"id": "Alex", "days": "5"})["removed"] == 1
        assert lw.purge({"id": "Alex", "days": 365})["removed"] == 0
        assert [e["address"] for e in ls.location_entries("Alex")] == ["1 days ago"]

        # days=0 purges everything logged up to now, leaving the file empty.
        zeroed = lw.purge({"id": "Alex", "days": 0})
        assert zeroed["removed"] == 1 and zeroed["kept"] == 0, zeroed
        assert ls.location_entries("Alex") == []
        assert ls.location_file("Alex").exists()      # the file itself stays
    print("  OK: only entries older than the window are removed")


def test_purge_ages_by_recorded_at_and_keeps_undated():
    """recorded_at wins over the caller's time; unparseable entries are kept."""
    print("Testing how purge ages entries...")
    with temp_location_dir():
        old = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime(
            ls.TIMESTAMP_FORMAT)[:-3]

        # A recent recorded_at with an ancient caller `time` must NOT be purged:
        # a phone with a wrong clock cannot make the server delete fresh data.
        lw.log({"id": "Alex", "latitude": 1, "longitude": 2, "time": "1999-01-01 00:00:00.000"})
        assert lw.purge({"id": "Alex", "days": 10})["removed"] == 0

        # An old recorded_at goes, whatever `time` says.
        _log_at("Alex", old, time="2099-01-01 00:00:00.000")
        assert lw.purge({"id": "Alex", "days": 10})["removed"] == 1

        # No recorded_at at all -> fall back to `time`.
        ls.save_locations("Sam", {"entries": [
            {"latitude": 1, "longitude": 2, "time": old},
            {"latitude": 3, "longitude": 4, "time": "2026-08-18T09:15:23Z"},  # ISO 8601
            {"latitude": 5, "longitude": 6, "time": "whenever"},              # undated
            {"latitude": 7, "longitude": 8},                                  # undated
        ]})
        res = lw.purge({"id": "Sam", "days": 10})
        assert res["removed"] == 1, res            # only the 40-day-old one
        assert res["undated"] == 2 and res["kept"] == 3, res
        assert "kept without a usable timestamp" in res["message"], res["message"]
        assert [e["latitude"] for e in ls.load_locations("Sam")["entries"]] == [3, 5, 7]
    print("  OK: aged by recorded_at, undated entries kept and reported")


def test_purge_validates_its_inputs_and_touches_one_id():
    """Bad `days`/`records` are errors, and a purge never touches another person."""
    print("Testing purge validation and isolation...")
    with temp_location_dir():
        old = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime(
            ls.TIMESTAMP_FORMAT)[:-3]
        _log_at("Alex", old)
        _log_at("Sam", old)

        for bad in ("ages", -1, "-0.5", "nan", "inf", [1]):
            res = lw.purge({"id": "Alex", "days": bad})
            assert res["status"] == "error" and res["error"] == "Invalid days", (bad, res)
        for bad in ("lots", -1, "-3", 1.5, "2.5", [1]):
            res = lw.purge({"id": "Alex", "records": bad})
            assert res["status"] == "error" and res["error"] == "Invalid records", (bad, res)
        assert lw.purge("nope")["error"] == "Invalid payload format"

        # Nothing was removed by any of the rejected calls.
        assert len(ls.location_entries("Alex")) == 1

        # Purging Alex leaves Sam's history and the presence store alone.
        with mock.patch.object(ps, "set_state") as set_state:
            assert lw.purge({"id": "Alex", "days": 10})["removed"] == 1
            set_state.assert_not_called()
        assert len(ls.location_entries("Sam")) == 1, "Sam's history was touched"

        # Blank/missing inputs are not errors — they mean "not asked for", so the
        # default record count applies.
        for blank in (None, "", "  "):
            res = lw.purge({"id": "Sam", "days": blank, "records": blank})
            assert res["mode"] == "records", (blank, res)
            assert res["records"] == lw.DEFAULT_RECORDS, (blank, res)
    print("  OK: bad days/records rejected, only the named id is purged")


def test_log_notifies_the_phone():
    """Logging a position pushes a notification through the HA wrapper."""
    print("Testing the location notification...")
    with temp_location_dir(), mock.patch.object(ln, "load_event_text", return_value={}):
        res = lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})
        assert res["status"] == "ok", res
        assert res["notify"]["status"] == "success", res["notify"]

        # The default title/message, with the placeholders filled in.
        title, message = ln.notify_phone.call_args[0]
        assert title == ln.DEFAULT_TEXT["title"] == "位置情報を記録", title
        assert message == "Alex is at Apple Park (2026-08-18 09:15:23.123).", message

        # A logged position with no address falls back to the coordinates, and
        # carries whatever time was stored (defaulted to now here).
        stored = lw.log({"id": "Alex", "latitude": 51.5014, "longitude": -0.1419})["location"]
        assert ln.notify_phone.call_args[0][1] == (
            f"Alex is at 51.5014,-0.1419 ({stored['time']})."), ln.notify_phone.call_args

        # New people are auto-registered as enabled, so they show up to toggle.
        assert ln.all_enabled() == {"Alex": True}, ln.all_enabled()
    print("  OK: notification sent with the address filled in")


def test_notification_text_comes_from_notify_config():
    """Title/message follow payload > notify_config.json > built-in default."""
    print("Testing the notification text precedence...")
    configured = {"title": "Seen", "message": "{id} at {address} ({latitude},{longitude})"}
    with temp_location_dir(), mock.patch.object(ln, "load_event_text", return_value=configured):
        lw.log({"id": "Alex", **APPLE_PARK})
        title, message = ln.notify_phone.call_args[0]
        assert title == "Seen", title
        assert message == "Alex at Apple Park (37.334606,-122.009102)", message

        # A request may override either one.
        lw.log({"id": "Alex", **APPLE_PARK, "title": "Custom {id}",
                "message": "map: {maps_url}"})
        title, message = ln.notify_phone.call_args[0]
        assert title == "Custom Alex", title
        assert message.startswith("map: https://maps.apple.com/?ll="), message
    print("  OK: payload beats notify_config.json beats the default")


def test_notification_switches_off_per_id():
    """Each id's switch silences only that id; the store is written regardless."""
    print("Testing the per-id notification switch...")
    with temp_location_dir():
        assert ln.set_enabled_for("Alex", False) is False
        assert ln.enabled_for("Alex") is False, ln.all_enabled()

        res = lw.log({"id": "Alex", **APPLE_PARK})
        assert res["notify"]["status"] == "skipped", res["notify"]
        assert "off for Alex" in res["notify"]["message"], res["notify"]
        ln.notify_phone.assert_not_called()
        # Switched off means "do not notify", not "do not log".
        assert lw.fetch({"id": "Alex"})["found"] is True

        # Sam is untouched by Alex's switch.
        assert lw.log({"id": "Sam", **APPLE_PARK})["notify"]["status"] == "success"
        assert ln.notify_phone.call_count == 1

        # And back on again.
        ln.set_enabled_for("Alex", True)
        assert lw.log({"id": "Alex", **APPLE_PARK})["notify"]["status"] == "success"
        assert ln.all_enabled() == {"Alex": True, "Sam": True}, ln.all_enabled()
    print("  OK: one id's switch does not affect another's")


def test_notify_phone_job_is_the_master_switch():
    """Disabling the notify_phone job silences every location notification."""
    print("Testing the master notification switch...")
    with temp_location_dir():
        with mock.patch.object(ln, "master_enabled", return_value=False) as master:
            res = lw.log({"id": "Alex", **APPLE_PARK})
            master.assert_called_once_with(ln.MASTER_SWITCH)
            assert ln.MASTER_SWITCH == "notify_phone", ln.MASTER_SWITCH
            assert res["notify"]["status"] == "skipped", res["notify"]
            assert "master 'notify_phone' switch" in res["notify"]["message"]
            ln.notify_phone.assert_not_called()
            # Still logged — the master switch is about notifications only.
            assert lw.fetch({"id": "Alex"})["found"] is True

        # The per-id switch is not even consulted, so nobody is auto-registered.
        assert ln.all_enabled() == {}, ln.all_enabled()
    print("  OK: the notify_phone job switch gates the lot")


def test_a_failing_notification_never_loses_the_position():
    """An unreachable Home Assistant is reported, not raised."""
    print("Testing notification failure handling...")
    with temp_location_dir():
        ln.notify_phone.side_effect = ValueError("Missing required configuration field")
        res = lw.log({"id": "Alex", **APPLE_PARK})
        assert res["status"] == "ok", res              # the write still succeeded
        assert res["notify"]["status"] == "error", res["notify"]
        assert "Missing required configuration" in res["notify"]["message"]
        assert ls.latest_location("Alex") is not None
    print("  OK: notification errors reported, position still stored")


def test_notification_endpoints():
    """The per-id switches are toggleable over HTTP."""
    print("Testing /location/notify endpoints...")
    client, headers = _client_and_headers()
    if not client:
        return

    with temp_location_dir():
        assert client.get("/location/notify").status_code == 401  # no secret

        lw.log({"id": "Alex", **APPLE_PARK})       # registers Alex
        listed = client.get("/location/notify", headers=headers).get_json()
        assert listed["ids"] == [{"id": "Alex", "enabled": True}], listed
        assert listed["master"]["job"] == "notify_phone", listed

        off = client.post("/location/notify/Alex/disable", headers=headers).get_json()
        assert off["enabled"] is False and off["id"] == "Alex", off
        assert ln.enabled_for("Alex") is False

        on = client.post("/location/notify/Alex/toggle", headers=headers).get_json()
        assert on["enabled"] is True, on
        assert client.post("/location/notify/Alex/toggle",
                           headers=headers).get_json()["enabled"] is False

        # A person nobody has logged yet can still be switched, and non-ASCII
        # ids survive the round trip through the URL.
        assert client.post(f"/location/notify/{ps.DEFAULT_PERSON}/disable",
                           headers=headers).get_json()["enabled"] is False
        assert ln.all_enabled()[ps.DEFAULT_PERSON] is False, ln.all_enabled()

        assert client.post("/location/notify/Alex/enable", headers=headers).status_code == 200
        assert client.get("/location/notify/Alex/enable", headers=headers).status_code == 405
    print("  OK: enable/disable/toggle and the listing all served")


def test_log_writes_a_file_per_id():
    """Each id gets its own <id>_loc.json, holding the four logged fields."""
    print("Testing log per-id files...")
    with temp_location_dir() as directory:
        res = lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})
        assert res["status"] == "ok", res
        assert res["id"] == "Alex" and res["file"] == "Alex_loc.json", res

        lw.log({"id": "Sam", "latitude": 51.5014, "longitude": -0.1419})

        files = sorted(p.name for p in directory.iterdir())
        assert files == ["Alex_loc.json", "Sam_loc.json"], files

        store = json.loads((directory / "Alex_loc.json").read_text(encoding="utf-8"))
        assert store["id"] == "Alex", store
        assert len(store["entries"]) == 1, store
        entry = store["entries"][0]
        assert entry["latitude"] == 37.334606 and entry["longitude"] == -122.009102, entry
        assert entry["address"] == "Apple Park", entry
        assert entry["time"] == "2026-08-18 09:15:23.123", entry  # stored verbatim
        assert entry["recorded_at"] and store["last_modified"], entry

        # Sam's file holds only Sam.
        sam = json.loads((directory / "Sam_loc.json").read_text(encoding="utf-8"))
        assert len(sam["entries"]) == 1 and sam["entries"][0]["latitude"] == 51.5014, sam
    print("  OK: one <id>_loc.json per person, with lat/lon/address/time")


def test_log_never_writes_to_the_text_logs():
    """A location must not land in default.log / blink.log / any log type."""
    print("Testing that log stays out of the text logs...")
    from jobs import log_engine

    with temp_location_dir(), mock.patch.object(log_engine, "log") as write_log:
        lw.log({"id": "Alex", **APPLE_PARK})
        lw.fetch({"id": "Alex"})
        write_log.assert_not_called()

    # A future refactor that pulls the logging engine into this job fails here.
    assert not hasattr(lw, "write_log"), "location_webhook must not write to the text logs"
    print("  OK: nothing written through the logging engine")


def test_log_defaults_id_time_and_address():
    """id defaults to the default person, time to now, address to null."""
    print("Testing log defaults...")
    with temp_location_dir() as directory:
        res = lw.log({"latitude": 37.3318, "longitude": -122.0312})
        assert res["id"] == ps.DEFAULT_PERSON, res

        entry = res["location"]
        assert entry["address"] is None, entry
        assert entry["time"] == entry["recorded_at"], entry  # defaulted to now

        # Non-ASCII ids are usable filenames and stay readable in the file.
        path = directory / f"{ps.DEFAULT_PERSON}_loc.json"
        assert path.exists(), sorted(p.name for p in directory.iterdir())
        assert ps.DEFAULT_PERSON in path.read_text(encoding="utf-8")
    print(f"  OK: defaults to {ps.DEFAULT_PERSON} / now / null address")


def test_log_accepts_strings_and_aliases():
    """Shortcuts send text; lat/lon/lng/long are accepted as aliases."""
    print("Testing log coercion and key aliases...")
    with temp_location_dir():
        res = lw.log({"id": "Alex", "latitude": "37.334606", "longitude": "-122.009102"})
        assert res["location"]["latitude"] == 37.334606, res
        assert res["location"]["longitude"] == -122.009102, res

        for lat_key in ("latitude", "lat"):
            for lon_key in ("longitude", "lon", "lng", "long"):
                res = lw.log({"id": "Alex", lat_key: 1.5, lon_key: 2.5})
                assert res["status"] == "ok", (lat_key, lon_key, res)
                assert res["location"]["latitude"] == 1.5, res
                assert res["location"]["longitude"] == 2.5, res

        # Blank strings count as missing.
        blank = lw.log({"id": "Alex", "latitude": " ", "longitude": "1"})
        assert blank["status"] == "error", blank
    print("  OK: numeric strings coerced, lat/lon/lng/long accepted")


def test_log_rejects_bad_payloads():
    """Missing/out-of-range/non-numeric coordinates are errors, and write nothing."""
    print("Testing log validation...")
    with temp_location_dir() as directory:
        cases = [
            ({}, "no coordinates at all"),
            ({"latitude": 37.3}, "longitude missing"),
            ({"longitude": -122.0}, "latitude missing"),
            ({"latitude": 91, "longitude": 0}, "latitude out of range"),
            ({"latitude": 0, "longitude": 181}, "longitude out of range"),
            ({"latitude": "north", "longitude": 0}, "non-numeric"),
            ({"latitude": "nan", "longitude": 0}, "nan"),
            ({"latitude": "inf", "longitude": 0}, "inf"),
            ({"latitude": True, "longitude": 0}, "boolean"),
            ({"latitude": None, "longitude": None}, "explicit nulls"),
        ]
        for payload, label in cases:
            res = lw.log({"id": "Alex", **payload})
            assert res["status"] == "error", (label, res)
            assert res["error"] == "Invalid coordinates", (label, res)
            assert res["message"], (label, res)

        not_object = lw.log("nope")
        assert not_object["error"] == "Invalid payload format", not_object
        assert lw.log(None)["error"] == "Invalid payload format"

        # Nothing was written for any of them.
        assert list(directory.iterdir()) == [], sorted(p.name for p in directory.iterdir())
        assert lw.fetch({"id": "Alex"})["found"] is False
    print("  OK: bad coordinates rejected, no file created")


def test_log_appends_history_and_caps_it():
    """Entries append newest-last and the file is trimmed to MAX_ENTRIES."""
    print("Testing log history and cap...")
    with temp_location_dir():
        for i in range(3):
            lw.log({"id": "Alex", "latitude": i, "longitude": i})
        entries = ls.load_locations("Alex")["entries"]
        assert [e["latitude"] for e in entries] == [0, 1, 2], entries
        assert ls.latest_location("Alex")["latitude"] == 2

        with mock.patch.object(ls, "MAX_ENTRIES", 3):
            lw.log({"id": "Alex", "latitude": 3, "longitude": 3})
            entries = ls.load_locations("Alex")["entries"]
            assert [e["latitude"] for e in entries] == [1, 2, 3], entries
    print("  OK: appended newest-last, oldest dropped at the cap")


def test_fetch_returns_the_latest_entry():
    """fetch returns the four fields of the most recently logged position."""
    print("Testing fetch...")
    with temp_location_dir():
        lw.log({"id": "Alex", "latitude": 1, "longitude": 2, "address": "Old place",
                "time": "2026-08-17 08:00:00.000"})
        lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})

        res = lw.fetch({"id": "Alex"})
        assert res["status"] == "ok" and res["found"] is True, res
        assert res["id"] == "Alex", res
        assert res["latitude"] == 37.334606, res
        assert res["longitude"] == -122.009102, res
        assert res["address"] == "Apple Park", res
        assert res["time"] == "2026-08-18 09:15:23.123", res
        assert res["maps_url"] == (
            "https://maps.apple.com/?ll=37.334606,-122.009102&q=Apple+Park"
        ), res
        assert res["google_maps_url"] == (
            "https://www.google.com/maps?q=37.334606,-122.009102"
        ), res
        assert res["message"] == "Alex was at Apple Park at 2026-08-18 09:15:23.123", res
        assert "entries" not in res, res  # only when n is asked for
    print("  OK: latest lat/lon/address/time plus a Maps URL")


def test_fetch_unknown_id_is_not_an_error():
    """An id with nothing logged reports found=false with null fields."""
    print("Testing fetch for an unknown id...")
    with temp_location_dir():
        res = lw.fetch({"id": "nobody"})
        assert res["status"] == "ok" and res["found"] is False, res
        for field in ("latitude", "longitude", "address", "time", "maps_url",
                      "google_maps_url"):
            assert res[field] is None, (field, res)
        assert res["message"] == "nobody: no location logged yet.", res

        # No id -> the default person; a non-dict payload does not raise.
        assert lw.fetch({})["id"] == ps.DEFAULT_PERSON
        assert lw.fetch(None)["id"] == ps.DEFAULT_PERSON
        assert lw.fetch("nope")["found"] is False
    print("  OK: unknown id reported as found=false, not an error")


def test_fetch_recent_history():
    """n returns that many recent entries, newest first."""
    print("Testing fetch history...")
    with temp_location_dir():
        for i in range(4):
            lw.log({"id": "Alex", "latitude": i, "longitude": i})

        res = lw.fetch({"id": "Alex", "n": "2"})  # query strings arrive as text
        assert [e["latitude"] for e in res["entries"]] == [3, 2], res
        assert lw.fetch({"id": "Alex", "n": 99})["entries"].__len__() == 4

        for bad in ("0", -1, "lots", True, None):
            assert "entries" not in lw.fetch({"id": "Alex", "n": bad}), bad
    print("  OK: ?n= returns recent entries newest first, bad values ignored")


def test_fetch_tolerates_a_hand_edited_file():
    """A file with missing/odd fields still reads, without a bogus Maps pin."""
    print("Testing fetch against odd stored entries...")
    with temp_location_dir():
        ls.save_locations("Alex", {"entries": [{"address": "Somewhere"}]})
        res = lw.fetch({"id": "Alex"})
        assert res["found"] is True and res["maps_url"] is None, res
        assert res["google_maps_url"] is None, res
        assert res["address"] == "Somewhere" and res["latitude"] is None, res

        # Corrupt JSON and unexpected shapes read as empty rather than raising.
        ls.location_file("Sam").write_text("{not json", encoding="utf-8")
        assert lw.fetch({"id": "Sam"})["found"] is False
        ls.location_file("Kim").write_text('{"entries": "nope"}', encoding="utf-8")
        assert lw.fetch({"id": "Kim"})["found"] is False
    print("  OK: odd/corrupt files read as empty, no invalid pin")


def test_id_cannot_escape_the_state_directory():
    """An id from an HTTP payload must never write outside state/."""
    print("Testing id sanitizing...")
    with temp_location_dir() as directory:
        for nasty in ("../../etc/passwd", "..", ".", "/absolute", "a\\b", "with\x00null",
                      "  ..  ", "nul:name"):
            res = lw.log({"id": nasty, **APPLE_PARK})
            assert res["status"] == "ok", (nasty, res)
            path = ls.location_file(nasty)
            assert path.parent.resolve() == directory.resolve(), (nasty, path)
            assert path.name.endswith(ls.FILE_SUFFIX), (nasty, path)

        # Everything landed inside the temp dir, nothing above it.
        written = sorted(p.name for p in directory.iterdir())
        assert all(name.endswith(ls.FILE_SUFFIX) for name in written), written
        assert not any("/" in name or name.startswith(".") for name in written), written

        # An id that sanitizes down to nothing gets the fallback stem.
        assert ls.location_file("..").name == f"{ls._FALLBACK_STEM}{ls.FILE_SUFFIX}"

        # Long ids are truncated to a legal filename length.
        long_id = "x" * 300
        assert len(ls._safe_stem(long_id)) == ls._MAX_STEM
        assert ls.location_file(long_id).name == f"{'x' * ls._MAX_STEM}{ls.FILE_SUFFIX}"
    print("  OK: path separators, dots, control chars and long ids neutralized")


def test_maps_url_encodes_labels():
    """Spaces, commas and non-ASCII in the label are percent-encoded for Maps."""
    print("Testing the map URLs...")
    url = lw.maps_url(37.334606, -122.009102, "娜's car")
    assert url.startswith("https://maps.apple.com/?ll=37.334606,-122.009102&q="), url
    assert "%E5%A8%9C%27s+car" in url, url
    assert " " not in url, url

    # The comma in ll is kept literal; a comma in the label is not.
    assert lw.maps_url(1.5, -2.5, "Apple Park, Cupertino") == (
        "https://maps.apple.com/?ll=1.5,-2.5&q=Apple+Park%2C+Cupertino"
    )
    # A missing label just omits q.
    assert lw.maps_url(1.5, -2.5) == "https://maps.apple.com/?ll=1.5,-2.5"

    # Google takes the coordinates as its query, comma left literal.
    assert lw.google_maps_url(37.334606, -122.009102) == (
        "https://www.google.com/maps?q=37.334606,-122.009102"
    )
    assert lw.google_maps_url(-33.8688, 151.2093) == (
        "https://www.google.com/maps?q=-33.8688,151.2093"
    )
    print("  OK: Apple label percent-encoded, Google q=lat,lon built")


def test_webhooks_are_registered():
    """config.json wires all four handlers up as one secret-protected job."""
    print("Testing location webhook registration...")
    config = json.loads((Path(__file__).parent.parent / "configs" / "config.json").read_text())
    hooks = {h["path"]: h for h in config["webhooks"]}
    for action in ("log", "fetch", "history", "purge"):
        path = f"/webhook/location/{action}"
        hook = hooks.get(path)
        assert hook, f"{path} not registered in configs/config.json"
        assert hook["module"] == "jobs.location_webhook", hook
        assert hook["function"] == action, hook
        assert hook["require_secret"] is True, hook
        assert callable(getattr(lw, action, None)), action

    # One module means one job switch, like presence_webhook.
    job_switches = json.loads(
        (Path(__file__).parent.parent / "configs" / "job_switches.json").read_text())
    assert "location_webhook" in job_switches["jobs"], job_switches
    print("  OK: /webhook/location/{log,fetch,history,purge} -> one job, secret required")


def test_location_endpoints():
    """GET /location and /location/history are served and need the secret."""
    print("Testing GET /location and /location/history...")
    client, headers = _client_and_headers()
    if not client:
        return

    with temp_location_dir():
        lw.log({"id": "Alex", **APPLE_PARK, "time": "2026-08-18 09:15:23.123"})
        lw.log({"id": "Alex", "latitude": 51.5014, "longitude": -0.1419,
                "address": "Buckingham Palace", "time": "2026-08-18 20:04:55.545"})

        for path in ("/location?id=Alex", "/location/history?id=Alex"):
            assert client.get(path).status_code == 401, path  # no secret

        # The JSON reader: latest position, ready for Maps.
        res = client.get("/location?id=Alex", headers=headers)
        assert res.status_code == 200, res.status_code
        body = res.get_json()
        assert body["found"] is True and body["address"] == "Buckingham Palace", body
        assert body["maps_url"].startswith("https://maps.apple.com/?ll="), body
        assert body["google_maps_url"] == (
            "https://www.google.com/maps?q=51.5014,-0.1419"), body

        # Unknown id is a normal 200 for Shortcuts, not a 404.
        unknown = client.get("/location?id=nobody", headers=headers)
        assert unknown.status_code == 200 and unknown.get_json()["found"] is False

        # The history reader: plain text, not JSON.
        history = client.get("/location/history?id=Alex", headers=headers)
        assert history.status_code == 200, history.status_code
        assert history.mimetype == "text/plain", history.mimetype
        text = history.get_data(as_text=True)
        assert text.startswith("Location history — Alex — 2 entries"), text
        assert "Buckingham Palace" in text and "Apple Park" in text, text
        assert text.endswith("\n"), repr(text[-20:])

        capped = client.get("/location/history?id=Alex&n=1", headers=headers)
        assert "— 1 of 2 entries" in capped.get_data(as_text=True)

        # Both readers also accept a JSON body.
        assert client.post("/location", headers=headers,
                           json={"id": "Alex"}).get_json()["latitude"] == 51.5014
        assert "Alex" in client.post("/location/history", headers=headers,
                                    json={"id": "Alex"}).get_data(as_text=True)
    print("  OK: JSON latest + plain text history served, 401 without the secret")


if __name__ == "__main__":
    test_every_handler_defaults_the_id()
    test_log_notifies_the_phone()
    test_notification_text_comes_from_notify_config()
    test_notification_switches_off_per_id()
    test_notify_phone_job_is_the_master_switch()
    test_a_failing_notification_never_loses_the_position()
    test_notification_endpoints()
    test_history_formats_all_entries_as_text()
    test_history_edge_cases()
    test_trigger_is_stored_and_shown_everywhere()
    test_trigger_is_optional_everywhere()
    test_trigger_notification_templates_are_configurable()
    test_webhook_accepts_query_params_and_a_headerless_body()
    test_purge_keeps_the_most_recent_records_by_default()
    test_purge_records_wins_over_days()
    test_purge_by_days_still_works()
    test_purge_ages_by_recorded_at_and_keeps_undated()
    test_purge_validates_its_inputs_and_touches_one_id()
    test_log_writes_a_file_per_id()
    test_log_never_writes_to_the_text_logs()
    test_log_defaults_id_time_and_address()
    test_log_accepts_strings_and_aliases()
    test_log_rejects_bad_payloads()
    test_log_appends_history_and_caps_it()
    test_fetch_returns_the_latest_entry()
    test_fetch_unknown_id_is_not_an_error()
    test_fetch_recent_history()
    test_fetch_tolerates_a_hand_edited_file()
    test_id_cannot_escape_the_state_directory()
    test_maps_url_encodes_labels()
    test_webhooks_are_registered()
    test_location_endpoints()
    print("\nAll location tests passed!")
