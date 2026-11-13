#!/usr/bin/env python3
"""Tests for the shared crash-safe file write (jobs/atomic_write.py).

Everything happens in a temp dir:

    python3 tests/test_atomic_write.py
"""

import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from jobs.atomic_write import write_atomic


def test_writes_the_text_and_creates_parent_folders():
    print("Testing write_atomic creates folders...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a" / "b" / "file.json"
        write_atomic(path, "héllo")
        assert path.read_text(encoding="utf-8") == "héllo"
        assert [p.name for p in path.parent.iterdir()] == ["file.json"]
    print("  OK")


def test_replaces_an_existing_file():
    print("Testing write_atomic replaces...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "file.txt"
        path.write_text("old")
        write_atomic(path, "new")
        assert path.read_text() == "new"
    print("  OK")


def test_a_failed_rename_leaves_the_original_and_no_temp_file():
    print("Testing write_atomic failure...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "file.txt"
        path.write_text("old")
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            try:
                write_atomic(path, "new")
            except OSError:
                pass
            else:
                raise AssertionError("expected OSError")
        assert path.read_text() == "old"
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["file.txt"]
    print("  OK")


def test_threads_writing_the_same_file_do_not_fail_each_other():
    print("Testing write_atomic with racing threads...")
    texts = [f"text-{i}" * 1000 for i in range(8)]
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "file.txt"

        def work(text):
            try:
                for _ in range(20):
                    write_atomic(path, text)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(t,)) for t in texts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert path.read_text() in texts
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["file.txt"]
    print("  OK")


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
    print(f"\nAll {len(tests)} atomic write tests passed!")
