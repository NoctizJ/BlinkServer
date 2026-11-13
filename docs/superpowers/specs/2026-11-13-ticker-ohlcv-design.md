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

## Storage module

`jobs/ticker_ohlcv_store.py` owns `stock/ohlcv/`: `SYMBOL_RE`, `merge_records`,
`load_records(symbol)` (`[]` if there is no file; `ValueError` for an invalid symbol or a
damaged file) and `save_merged(symbol, records)`, which reads, merges and writes under one
lock and returns `(days it had before, all records now)`. The fetcher, the daily job and the
repair tool all go through it. `ticker_ohlcv.fetch_bars(symbol, start, end)` is the public
"records straight from Yahoo" helper (the repair tool uses it for the QQQ calendar).

## Daily job

`run_daily` (scheduled from `configs/ticker_ohlcv_schedule.json`: 14:30 America/Los_Angeles,
weekdays, `catch_up_until` 23:00) fetches the last 7 days (`DAILY_LOOKBACK_DAYS`, today
included) for `CurrentQQQ`, through the same `fetch_many` as the webhook. The job switch is
the module name, `ticker_ohlcv`, the same as the fetch webhook.

Decisions and reasons:

- **A 7-day look-back, not today only.** The first design fetched only today, so a ticker
  that failed, a day the Mac slept through, or a day Yahoo had not yet published stayed
  missing until someone ran repair. With the look-back every run heals the last week; the
  merge makes the re-fetch harmless.
- **No holiday detection.** An earlier version treated "every ticker has no data" as a market
  holiday (`no_new_data`, done, no retry). yfinance raises `YFPricesMissingError` for ANY Yahoo
  error response (a bad crumb, say), so a Yahoo outage looked like a holiday and was never
  retried. Now a failed ticker is just failed, a holiday adds nothing (the other days of the
  window are re-fetched anyway), and a run that saved nothing is an `error` the scheduler
  retries every 15 minutes until 23:00; a run that saved anything is done, `partial` included.
- **"Today" is the schedule's date**, not the server's: `_schedule_today` uses the schedule's
  time zone, the same day the scheduler records the run under.
- **Failures are logged** (the first 10, then "+N more"), because the scheduler logs only the
  run's status.
- An empty saved QQQ list is its own error ("the saved QQQ holdings list is empty"), not
  "tickers is required".

The two schedulers (QQQ holdings and this job) share `state/scheduler.json`, so recording a
run holds a module-level lock around the read-modify-write; threads are named
`blink-scheduler-<job>`.

## Repair

`jobs/ticker_ohlcv_repair.py` (CLI, and `POST /webhook/ticker/ohlcv/repair`, with its own
switch `ticker_ohlcv_repair`).

- **The calendar is QQQ's own bars over the range**: one extra request, and no holiday list to
  keep current. A QQQ outage, or QQQ having no bar at all in the range, is an `error` rather
  than a guess. A single day QQQ lacks is not in the calendar, so it is silently not checked.
- **A hole** is a calendar day with no bar in a ticker's file (a ticker with no file is missing
  every day, so repair also backfills). Each ticker with holes is fetched once over first..last
  hole, merged, and re-checked from disk, so the report shows what actually landed: holes Yahoo
  cannot supply stay `still_missing`. A ticker listed after the range start therefore stays
  `partial` on every run.
- **Today's bar is not trusted before 17:00 New York time.** Yahoo's day bar is partial until
  the close (16:00 ET) and final by 17:00; saving it would store a half-finished day that no
  later check notices, because the day then is not a hole. The check stops at
  `last_final_day`, and a range that ends after it is clamped (the message says so); a range
  that lies entirely after it is an `error`.
- Status: `ok` (nothing missing, or all filled), `partial` (holes remain, or a ticker could not
  be checked, such as a damaged file), `error` (bad arguments, nothing to check yet, or no
  calendar).

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

Also not verified from the sandbox: real Yahoo behaviour for the daily and repair
requests, the time 101 requests take, and whether Yahoo has the full day's bar at 14:30
Pacific.
