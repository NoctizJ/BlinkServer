#!/usr/bin/env python3
"""Tests for the QQQ holdings storage (jobs/qqqholdings_store.py).

Nothing touches the repo: stock/qqqholdings/ is redirected to a temp dir:

    python3 tests/test_qqqholdings_store.py
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import jobs.qqqholdings_store as qs


class temp_store:
    """Point the store at a temp folder (which does not exist yet, like a fresh install)."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "stock" / "qqqholdings"
        self._patch = mock.patch.object(qs, "STOCK_DIR", self.dir)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()
        return False

    def files(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.is_dir() else []


HOLDINGS = {
    "NVDA": {"name": "NVIDIA Corp", "weight": 9.2, "shares": 100.0},
    "NESN": {"name": "Société Générale Zürich", "weight": 0.4, "shares": 4.0},
}
REPORT = {"as_of": "2026-10-30", "previous_as_of": None, "baseline": True,
          "added": [], "removed": [], "changed": [], "note": "café"}
TEXT = "QQQ holdings — 2026-10-30\nBaseline saved: 2 holdings."


def test_snapshot_dates_lists_only_snapshot_files_sorted():
    print("Testing snapshot_dates...")
    with temp_store() as store:
        store.dir.mkdir(parents=True)
        for name in ("qqqholdings_2026-10-30.json", "qqqholdings_2026-10-28.json",
                     "qqqholdings_2026-11-02.json",
                     "qqqholdings_report_2026-10-30.json", "qqqholdings_report_2026-10-30.txt",
                     ".qqqholdings_x.tmp", ".qqqholdings_2026-11-03.json.tmp",
                     "qqqholdings_2026-10-31.txt", "qqqholdings_2026-1-5.json",
                     "qqqholdings_2026-10-29.json.bak", "notes.json"):
            (store.dir / name).write_text("{}")
        dates = qs.snapshot_dates()
        assert dates == ["2026-10-28", "2026-10-30", "2026-11-02"], dates
    print("  OK")


def test_snapshot_dates_with_no_folder_or_an_empty_one():
    print("Testing snapshot_dates without a folder...")
    with temp_store() as store:
        assert not store.dir.exists()
        assert qs.snapshot_dates() == []
        store.dir.mkdir(parents=True)
        assert qs.snapshot_dates() == []
    print("  OK")


def test_save_day_writes_exactly_three_files_with_the_expected_content():
    print("Testing save_day content...")
    with temp_store() as store:
        qs.save_day("2026-10-30", HOLDINGS, REPORT, TEXT)
        assert store.files() == ["qqqholdings_2026-10-30.json",
                                 "qqqholdings_report_2026-10-30.json",
                                 "qqqholdings_report_2026-10-30.txt"], store.files()
        snapshot = (store.dir / "qqqholdings_2026-10-30.json").read_text(encoding="utf-8")
        assert snapshot == json.dumps({"as_of": "2026-10-30", "holdings": HOLDINGS},
                                      indent=2, ensure_ascii=False), snapshot
        report = (store.dir / "qqqholdings_report_2026-10-30.json").read_text(encoding="utf-8")
        assert report == json.dumps(REPORT, indent=2, ensure_ascii=False), report
        text = (store.dir / "qqqholdings_report_2026-10-30.txt").read_text(encoding="utf-8")
        assert text == TEXT + "\n" and text.endswith("\n") and not text.endswith("\n\n"), repr(text)
    print("  OK")


def test_non_ascii_names_survive_unescaped():
    print("Testing non-ASCII...")
    with temp_store() as store:
        qs.save_day("2026-10-30", HOLDINGS, REPORT, TEXT)
        raw = (store.dir / "qqqholdings_2026-10-30.json").read_bytes()
        assert "Société Générale Zürich".encode("utf-8") in raw, raw
        assert b"\\u" not in raw, raw
        assert qs.load_snapshot("2026-10-30")["NESN"]["name"] == "Société Générale Zürich"
    print("  OK")


def test_save_day_writes_the_snapshot_last():
    print("Testing the snapshot is written last...")
    real = qs.write_atomic
    order = []

    def recording(path, text):
        order.append(path.name)
        real(path, text)

    with temp_store():
        with mock.patch.object(qs, "write_atomic", side_effect=recording):
            qs.save_day("2026-10-30", HOLDINGS, REPORT, TEXT)
    assert len(order) == 3, order
    assert order[-1] == "qqqholdings_2026-10-30.json", order
    assert set(order[:2]) == {"qqqholdings_report_2026-10-30.json",
                              "qqqholdings_report_2026-10-30.txt"}, order
    print("  OK")


def test_a_failed_second_write_leaves_no_snapshot():
    print("Testing a crash before the snapshot...")
    real = qs.write_atomic

    def flaky(path, text):
        if path.name == "qqqholdings_report_2026-10-30.txt":
            raise OSError("disk full")
        real(path, text)

    with temp_store() as store:
        with mock.patch.object(qs, "write_atomic", side_effect=flaky):
            try:
                qs.save_day("2026-10-30", HOLDINGS, REPORT, TEXT)
            except OSError as exc:
                assert "disk full" in str(exc)
            else:
                raise AssertionError("save_day swallowed the OSError")
        assert not (store.dir / "qqqholdings_2026-10-30.json").exists(), store.files()
        assert qs.snapshot_dates() == [], qs.snapshot_dates()
    print("  OK")


def test_load_snapshot_round_trips():
    print("Testing load_snapshot round trip...")
    with temp_store():
        qs.save_day("2026-10-30", HOLDINGS, REPORT, TEXT)
        assert qs.load_snapshot("2026-10-30") == HOLDINGS
    print("  OK")


def test_load_snapshot_raises_for_missing_or_damaged_files():
    print("Testing load_snapshot failures...")
    with temp_store() as store:
        store.dir.mkdir(parents=True)

        def raises(date, kinds):
            try:
                qs.load_snapshot(date)
            except kinds:
                return
            raise AssertionError(f"load_snapshot({date}) did not raise {kinds}")

        raises("2026-10-30", OSError)                       # missing
        (store.dir / "qqqholdings_2026-10-29.json").write_text("{oops")
        raises("2026-10-29", ValueError)                    # bad JSON
        (store.dir / "qqqholdings_2026-10-28.json").write_text("{}")
        raises("2026-10-28", KeyError)                      # no "holdings"
        (store.dir / "qqqholdings_2026-10-27.json").write_text("[1]")
        raises("2026-10-27", TypeError)                     # a list, not an object
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} QQQ holdings store tests passed!")
