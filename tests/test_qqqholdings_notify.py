#!/usr/bin/env python3
"""Tests for the QQQ holdings phone notification (jobs/qqqholdings_notify.py).

Every send is mocked, so nothing reaches Home Assistant or a phone:

    python3 tests/test_qqqholdings_notify.py
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.qqqholdings_notify as qn

OK = {"status": "success", "message": "sent"}


def _report(added=(), removed=()):
    return {"as_of": "2026-11-02", "previous_as_of": "2026-10-30",
            "added": [{"ticker": t, "name": n, "weight": w} for t, n, w in added],
            "removed": [{"ticker": t, "name": n, "weight": w} for t, n, w in removed]}


class patched:
    """Patch the send, the master switch and the notify_config.json text."""

    def __init__(self, send=None, master=True, text=None):
        self.send = send or mock.Mock(return_value=dict(OK))
        self.master, self.text = master, text if text is not None else {}

    def __enter__(self):
        self._patches = [
            mock.patch.object(qn, "notify_phone", self.send),
            mock.patch.object(qn, "master_enabled", mock.Mock(return_value=self.master)),
            mock.patch.object(qn, "load_event_text",
                              self.text if isinstance(self.text, mock.Mock)
                              else mock.Mock(return_value=self.text)),
        ]
        for p in self._patches:
            p.start()
        return self.send

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def test_added_only():
    print("Testing added only...")
    with patched() as send:
        res = qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)]))
    send.assert_called_once_with("QQQ holdings changed — 2026-11-02",
                                 "Added: ZZZZ (Example New Co, 0.42%)",
                                 event="qqqholdings_change")
    assert res["status"] == "success" and res["title"] == "QQQ holdings changed — 2026-11-02", res
    print("  OK")


def test_removed_only():
    print("Testing removed only...")
    with patched() as send:
        qn.notify_changes(_report(removed=[("ALNY", "Alnylam Pharmaceuticals Inc", 0.10)]))
    assert send.call_args.args[1] == "Removed: ALNY (Alnylam Pharmaceuticals Inc, was 0.10%)"
    assert send.call_args.kwargs == {"event": "qqqholdings_change"}
    print("  OK")


def test_both_sides_in_order_and_comma_separated():
    print("Testing added and removed...")
    with patched() as send:
        qn.notify_changes(_report(
            added=[("ZZZZ", "Example New Co", 0.42), ("YYYY", "Other Co", 0.30)],
            removed=[("ALNY", "Alnylam Pharmaceuticals Inc", 0.10)]))
    assert send.call_args.args[1] == (
        "Added: ZZZZ (Example New Co, 0.42%), YYYY (Other Co, 0.30%)\n"
        "Removed: ALNY (Alnylam Pharmaceuticals Inc, was 0.10%)"), send.call_args.args[1]
    print("  OK")


def test_nothing_added_or_removed_is_skipped():
    print("Testing a weight-only report...")
    with patched() as send:
        res = qn.notify_changes(_report())
    assert res["status"] == "skipped", res
    assert send.call_count == 0
    print("  OK")


def test_master_switch_off_is_skipped():
    print("Testing the master switch...")
    with patched(master=False) as send:
        res = qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)]))
    assert res["status"] == "skipped" and "notify_phone" in res["message"], res
    assert send.call_count == 0
    print("  OK")


def test_send_raising_gives_an_error_result():
    print("Testing a raising send...")
    with patched(send=mock.Mock(side_effect=ValueError("no config"))):
        res = qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)]))
    assert res["status"] == "error" and res["message"] == "no config", res
    print("  OK")


def test_send_error_result_is_passed_through():
    print("Testing a send error result...")
    failed = {"status": "error", "message": "HTTP 401", "devices": []}
    with patched(send=mock.Mock(return_value=failed)):
        res = qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)]))
    assert res["status"] == "error" and res["message"] == "HTTP 401", res
    assert res["devices"] == [] and res["title"].startswith("QQQ holdings changed"), res
    print("  OK")


def test_notify_config_override_fills_every_placeholder():
    print("Testing the notify_config.json override...")
    text = {"title": "{as_of} vs {previous_as_of}",
            "message": "+{added_count} -{removed_count}\n{changes}"}
    with patched(text=text) as send:
        qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)],
                                  removed=[("ALNY", "Alnylam", 0.10), ("OLD", "Old Co", 0.2)]))
    title, message = send.call_args.args
    assert title == "2026-11-02 vs 2026-10-30", title
    assert message == ("+1 -2\nAdded: ZZZZ (Example New Co, 0.42%)\n"
                       "Removed: ALNY (Alnylam, was 0.10%), OLD (Old Co, was 0.20%)"), message
    print("  OK")


def test_load_event_text_raising_gives_an_error_result():
    print("Testing a raising load_event_text...")
    with patched(text=mock.Mock(side_effect=OSError("disk"))) as send:
        res = qn.notify_changes(_report(added=[("ZZZZ", "Example New Co", 0.42)]))
    assert res["status"] == "error" and res["message"] == "disk", res
    assert send.call_count == 0
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} QQQ holdings notify tests passed!")
