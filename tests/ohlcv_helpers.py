"""Helpers shared by the OHLCV tests (test_ticker_ohlcv.py, test_ticker_ohlcv_store.py,
test_ticker_ohlcv_repair.py). Import it after the repo root is on sys.path:

    from ohlcv_helpers import raises, rec, temp_ohlcv, ...

``python3 tests/<file>.py`` puts tests/ on sys.path, which is what makes that import work.
"""

import contextlib
import json
import tempfile
from pathlib import Path
from unittest import mock

import jobs.qqqholdings_store as qs
import jobs.ticker_ohlcv_store as store


class temp_ohlcv:
    """Point the store at a temp stock/ohlcv dir (which does not exist yet, like a fresh install).

    Each module passed in has its ``write_log`` mocked; ``log`` is the mock of the LAST one (None if
    none were passed)."""

    def __init__(self, *log_modules):
        self._log_modules = log_modules

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / "stock" / "ohlcv"
        self._patches = [mock.patch.object(store, "OHLCV_DIR", self.dir)]
        self._patches += [mock.patch.object(module, "write_log") for module in self._log_modules]
        started = [p.start() for p in self._patches]
        self.log = started[-1] if self._log_modules else None
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        return False

    def files(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.is_dir() else []

    def everything(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))


def raises(exc_type, fn, *args):
    try:
        fn(*args)
    except exc_type as exc:
        return exc
    raise AssertionError(f"{fn.__name__}{args} did not raise {exc_type.__name__}")


def rec(date, o, h, l, c, v):
    return {"date": date, "open": o, "high": h, "low": l, "close": c, "volume": v}


REC1 = [rec("2026-11-10", 10.0, 11.0, 9.0, 10.5, 1000), rec("2026-11-11", 10.5, 12.0, 10.0, 11.0, 2000)]


def holding(name, weight):
    return {"name": name, "weight": weight, "shares": 1.0}


def write_snapshot(folder, as_of, holdings):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"qqqholdings_{as_of}.json").write_text(
        json.dumps({"as_of": as_of, "holdings": holdings}), encoding="utf-8")


@contextlib.contextmanager
def temp_qqq(holdings=None):
    """The QQQ holdings dir (what CurrentQQQ reads) redirected to a temp dir; yields the folder.

    Without ``holdings`` the folder does not exist yet (no snapshot saved); with them, they are saved
    as the 2026-10-30 snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "stock" / "qqqholdings"
        if holdings is not None:
            write_snapshot(folder, "2026-10-30", holdings)
        with mock.patch.object(qs, "STOCK_DIR", folder):
            yield folder
