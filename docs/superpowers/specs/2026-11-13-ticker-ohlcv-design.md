# Ticker OHLCV history — design

Date: 2026-11-13

## What it does

`jobs/ticker_ohlcv.py` fetches daily open/high/low/close/volume history from Yahoo
Finance (through `yfinance`) and keeps one JSON file per ticker. It is both a command
line (`python3 -m jobs.ticker_ohlcv 30d AAPL,GOOGL`) and a webhook
(`POST /webhook/ticker/ohlcv/fetch`, secret required). The job switch and log type are
both `ticker_ohlcv`.

## Parameters

- `range`: `30d` or `30` (from 30 days ago through today), `START..END` (both dates
  included) or `START..` (through today). Dates are strictly `YYYY-MM-DD`; a start after
  the end, a start in the future, a date that does not exist and `0d` are rejected.
- `tickers`: a comma/space list (`AAPL,GOOGL`, `[AAPL, GOOGL]`, quoted items, or a JSON
  list), upper-cased and de-duplicated in order; or `CurrentQQQ` (any case), meaning every
  ticker of the newest saved QQQ holdings snapshot, read through `qqqholdings_job.read({})`.
  With no snapshot saved the call fails and points at `/webhook/qqqholdings/run`.

## Storage and merge

`stock/ohlcv/<SYMBOL>.json` is a list of `{"date", "open", "high", "low", "close",
"volume"}` sorted by date, written atomically (temp file, then rename, in `jobs/atomic_write.py`) under a lock.
Bars use `auto_adjust=False`; prices are rounded to 4 decimals; a day with a missing
price is skipped. Yahoo's `end` is exclusive, so the download asks for end + 1 day.

A fetch MERGES by date (decided by the owner): new days are added, days fetched again are
refreshed, nothing is dropped. Re-running is therefore safe. The symbol is the file name,
so only `[A-Z0-9^][A-Z0-9.\-=^]{0,14}` is accepted; anything path-like is a failed ticker.

## Failure handling

One bad ticker never stops the others. An unknown symbol or empty result, a download
error, a damaged existing file (left untouched) or a write error marks that ticker
`failed` with a reason. Result `status`: `ok` (all saved), `partial` (some failed),
`error` (none saved, or invalid arguments, in which case nothing is downloaded). The CLI
exits 0 only on `ok`. `yfinance` is imported only inside `_download`, so the server and
the other jobs work without it installed.

## Errors from Yahoo

`_download` calls `history(..., raise_errors=True)`. By default yfinance hides a network
failure or a rate limit (it prints "possibly delisted; no price data found" and returns an
empty frame), which would look like an unknown symbol and mislabel good tickers during a
rate-limited `CurrentQQQ` run. With `raise_errors=True` the real cause (a network error,
`YFRateLimitError`, `YFPricesMissingError` for an unknown symbol) is reported as that
ticker's failure reason, and the other tickers carry on. Re-running is safe because of the
merge, so failed tickers can simply be fetched again. An empty frame now only means no
trading days in the range.

Verified against yfinance 0.2.65's real `history()` signature (`raise_errors` is in it,
hence `yfinance>=0.2.65` in requirements.txt). The real library was called once from the
sandbox, where Yahoo is blocked: it surfaced a clean network error, not a TypeError. Real
Yahoo data was never fetched.

## The QQQ holdings move

The QQQ holdings files now live in `stock/qqqholdings/`; see the Storage section of the QQQ holdings spec (`2026-11-02-qqqholdings-daily-report-design.md`).

## Tested and not tested

Tested (`tests/test_ticker_ohlcv.py`, `tests/test_qqqholdings.py`): range and ticker
parsing, `CurrentQQQ` against real snapshot files in a temp dir, record conversion,
merging, the arguments `_download` passes to a fake `yfinance`, saving, merging, failures,
corrupt files, invalid symbols, the status values, the CLI, the registration, concurrent
fetches of one ticker. All downloads are faked.

Not tested: real Yahoo calls (the development sandbox blocks Yahoo), the real shape of
yfinance's frame beyond what the fakes imitate, and how long a `CurrentQQQ` fetch of
about 100 tickers takes.
