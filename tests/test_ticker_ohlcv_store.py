#!/usr/bin/env python3
"""Tests for the OHLCV storage (jobs/ticker_ohlcv_store.py).

Nothing touches the repo: stock/ohlcv is redirected to a temp dir:

    python3 tests/test_ticker_ohlcv_store.py
"""

import json
import sys
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import jobs.ticker_ohlcv_store as store
from ohlcv_helpers import REC1, raises, rec, temp_ohlcv


# -------------------------------------------------------------- merge_records

def test_merge_records():
    print("Testing merge_records...")
    old = [rec("2026-11-12", 3, 3, 3, 3, 3), rec("2026-11-10", 1, 1, 1, 1, 1),
           rec("2026-11-11", 2, 2, 2, 2, 2)]
    new = [rec("2026-11-13", 9, 9, 9, 9, 9), rec("2026-11-11", 5, 5, 5, 5, 5)]
    out = store.merge_records(old, new)
    assert [r["date"] for r in out] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"], out
    assert out[1] == rec("2026-11-11", 5, 5, 5, 5, 5), out
    assert out[0]["open"] == 1 and out[2]["open"] == 3
    assert store.merge_records([], new) == sorted(new, key=lambda r: r["date"])
    assert store.merge_records(old, []) == sorted(old, key=lambda r: r["date"])
    print("  OK")


# ---------------------------------------------------------------- load_records

def test_load_records_missing_file_is_empty():
    print("Testing load_records without a file...")
    with temp_ohlcv() as t:
        assert store.load_records("AAPL") == []
        assert t.everything() == [], t.everything()   # reading creates nothing
    print("  OK")


def test_load_records_reads_a_saved_file():
    print("Testing load_records...")
    with temp_ohlcv() as t:
        t.dir.mkdir(parents=True)
        (t.dir / "AAPL.json").write_text(json.dumps(REC1), encoding="utf-8")
        assert store.load_records("AAPL") == REC1
    print("  OK")


def test_load_records_rejects_a_damaged_file():
    print("Testing load_records damaged file...")
    for damage in ("{oops", "{}", "[1]", '{"date": "x"}', '[{"close": 1}]'):
        with temp_ohlcv() as t:
            t.dir.mkdir(parents=True)
            (t.dir / "AAPL.json").write_text(damage, encoding="utf-8")
            raises(ValueError, store.load_records, "AAPL")   # JSONDecodeError is a ValueError
            assert (t.dir / "AAPL.json").read_text(encoding="utf-8") == damage
    print("  OK")


def test_invalid_symbols_are_rejected_and_nothing_outside_the_dir_is_touched():
    print("Testing invalid symbols...")
    with temp_ohlcv() as t:
        for symbol in ("../x", "A/B", ".hidden", "", "A B", "..", "A\\B", "X" * 40):
            raises(ValueError, store.load_records, symbol)
            raises(ValueError, store.save_merged, symbol, REC1)
        assert t.everything() == [], t.everything()
    print("  OK")


def test_symbol_re_accepts_real_symbol_shapes():
    print("Testing SYMBOL_RE real symbol shapes...")
    for symbol in ("AAPL", "BRK-B", "BF.B", "^GSPC", "EURUSD=X", "QQQ"):
        assert store.SYMBOL_RE.match(symbol), symbol
    print("  OK")


# ----------------------------------------------------------------- save_merged

def test_save_merged_creates_the_file():
    print("Testing save_merged create...")
    with temp_ohlcv() as t:
        assert store.save_merged("AAPL", REC1) == (0, REC1)
        text = (t.dir / "AAPL.json").read_text(encoding="utf-8")
        assert text == json.dumps(REC1, indent=2), text
        assert t.files() == ["AAPL.json"], t.files()   # no leftover temp file
    print("  OK")


def test_save_merged_merges_overlapping_saves():
    print("Testing save_merged merge...")
    second = [rec("2026-11-11", 20.0, 21.0, 19.0, 20.5, 5000),
              rec("2026-11-12", 21.0, 22.0, 20.0, 21.5, 6000)]
    with temp_ohlcv() as t:
        store.save_merged("AAPL", REC1)
        before, merged = store.save_merged("AAPL", second)
        assert before == 2 and len(merged) == 3, (before, merged)
        want = [REC1[0], second[0], second[1]]
        assert merged == want, merged
        assert json.loads((t.dir / "AAPL.json").read_text()) == want
        assert store.load_records("AAPL") == want
        # A shorter save never drops history.
        assert store.save_merged("AAPL", [REC1[0]])[0] == 3
        assert len(store.load_records("AAPL")) == 3
    print("  OK")


def test_save_merged_leaves_a_damaged_file_untouched():
    print("Testing save_merged damaged file...")
    for damage in ("{oops", "[1]", '{"date": "x"}', '[{"close": 1}]'):
        with temp_ohlcv() as t:
            t.dir.mkdir(parents=True)
            (t.dir / "AAPL.json").write_text(damage, encoding="utf-8")
            raises(ValueError, store.save_merged, "AAPL", REC1)
            assert (t.dir / "AAPL.json").read_text(encoding="utf-8") == damage
            assert t.files() == ["AAPL.json"], t.files()
    print("  OK")


# ---------------------------------------------------------------- concurrency

def test_concurrent_saves_of_one_ticker_keep_every_date():
    print("Testing concurrent saves...")
    real = store.write_atomic

    def slow(path, text):
        time.sleep(0.03)
        real(path, text)

    days = [f"2026-11-{n:02d}" for n in range(1, 9)]
    errors = []

    def worker(day):
        try:
            store.save_merged("AAPL", [rec(day, 1.0, 2.0, 0.5, 1.5, 10)])
        except BaseException as exc:  # reported below, not lost in the thread
            errors.append(exc)

    with temp_ohlcv() as t, mock.patch.object(store, "write_atomic", side_effect=slow):
        threads = [threading.Thread(target=worker, args=(day,)) for day in days]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        saved = json.loads((t.dir / "AAPL.json").read_text())
        assert [r["date"] for r in saved] == days, saved
        assert t.files() == ["AAPL.json"], t.files()
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} OHLCV store tests passed!")
