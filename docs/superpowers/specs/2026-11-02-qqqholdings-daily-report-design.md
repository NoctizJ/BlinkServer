# QQQ holdings daily report — design

Date: 2026-11-02
Status: implemented

## Goal

Every weekday at 6:30 AM, BlinkServer downloads the QQQ holdings list from Invesco,
compares it with the previous snapshot, and saves a report of which tickers were
added, which were removed, and how the weight of each remaining ticker changed.
The holdings and the report are readable over webhooks. Broadcasting it as an iMessage is a later
step and out of scope here.

## Non-goals

- Sending the report anywhere (iMessage, phone notification). The `read` and `diff` webhooks are
  the hand-off point for that later step.
- A market-holiday calendar. The as-of date in Invesco's file covers holidays (see
  Data flow).
- Any ETF other than QQQ.

## Components

| File | Purpose | Depends on |
| ---- | ------- | ---------- |
| `jobs/scheduler.py` | Runs one job daily at HH:MM in a timezone, weekdays only, from a thread that `app.py`'s `__main__` block starts once. Handles retry, catch-up and sleep (see Failure handling). | `configs/schedule.json` |
| `jobs/qqqholdings_fetch.py` | Downloads QQQ holdings from Invesco and normalizes them to `{ticker: {name, weight, shares}}` plus the file's as-of date. | `requests` |
| `jobs/qqqholdings_diff.py` | Pure function: old map + new map -> `added`, `removed`, `changed`. No I/O. | nothing |
| `jobs/qqqholdings_job.py` | Orchestration (fetch, load previous, diff, save, log). Exposes `run` (manual trigger), `read` and `diff` webhooks. | the three above, log engine, switches |
| `configs/schedule.json` | The schedule: time, timezone, weekdays_only, catch_up_until. | |
| `tests/test_qqqholdings.py` | Tests for diff, parsing, fetch retry, job flow, idempotence, registration. (Scheduler tests are in `tests/test_scheduler.py`.) | |

The three webhooks are registered in `configs/config.json` like the existing jobs:
`/webhook/qqqholdings/run`, `/webhook/qqqholdings/read` and `/webhook/qqqholdings/diff`,
all with `require_secret: true`.

## Data flow

1. The scheduler fires at 6:30 in the configured timezone on Monday-Friday.
2. `qqqholdings_job.run` fetches the holdings (3 attempts with backoff).
3. The as-of date comes from Invesco's file, not from the clock. If it equals the
   latest snapshot's date (market holiday, or Invesco has not published yet), the
   run logs "no new data" and writes nothing.
4. Otherwise the job loads the previous snapshot, the latest file in `stock/` by
   as-of date. "Yesterday" therefore means the previous snapshot on disk: Monday's
   run compares against Friday.
5. With no previous snapshot (first ever run) it saves a baseline and no diff.
6. The diff and report are written, then logged.

Weight changes are in percentage points. A change smaller than a configurable
threshold (default 0.01 pp) is ignored as noise.

### Storage (`stock/`, at the BlinkServer root next to `state/`)

- `stock/qqqholdings_<asof>.json` — the day's snapshot
- `stock/qqqholdings_report_<asof>.json` — structured report
- `stock/qqqholdings_report_<asof>.txt` — the same report as readable text

All writes are atomic (temp file, then rename), so a crash never leaves a partial
snapshot that the next run would diff against.

### Read and diff webhooks

`/webhook/qqqholdings/read` returns the holdings of one snapshot, ranked from the
highest weight to the lowest (ties by ticker): the newest snapshot, or the one for a
given `date` (YYYY-MM-DD). Each row has `rank`, `ticker`, `name`, `weight` and
`shares`; the response also carries `count`, `total_weight` and a text table in
`message`. An unknown date is `not_found`.

`/webhook/qqqholdings/diff` returns a report (added / removed / weight changes)
computed from the saved snapshots, not read back from a saved report file. With no
params it compares the newest snapshot with the second newest. With both `first_date`
(the older, base side) and `second_date` (the newer side) it compares those two
snapshots, literally in the order given: reversed dates give the inverse report, they
are not swapped. Giving only one of the two is an error, and fewer than two saved
snapshots (or an unknown date) is `not_found`. The optional `threshold` is validated
exactly as for `run`. `run` still saves the daily report files; `diff` is what makes
the comparison available for any pair of saved dates. The later iMessage step can use
either webhook unchanged.

## Failure handling

- Fetch failures retry 3 attempts with backoff, then log through the existing log
  engine. No partial snapshot is written.
- Startup catch-up: if the server starts after 6:30 on a weekday, before a cutoff of
  12:00, and no run has happened for today, it runs once.
- Retry: a run that fails (Invesco unreachable, an error result) is tried again every
  15 minutes until it succeeds or the 12:00 cutoff passes. Added during implementation
  review: without it, a few minutes of Invesco downtime at 6:30 lost the whole day.
- Sleep: if the Mac slept through 6:30, the missed run happens on wake only if that is
  still before 12:00 on a weekday; otherwise the slot is skipped.
- A job that is switched off at its slot is skipped for the day (not retried).
- On/off switch in `configs/job_switches.json`, like the other jobs.

## Phone notification

- **When:** after a `run` that found new data in which at least one ticker was added to
  or removed from the fund. Not for weight-only changes, the first baseline run,
  `no_new_data` or errors. The scheduled 06:30 run and a manual
  `POST /webhook/qqqholdings/run` both go through `run`, so both can notify.
- **Text:** title `QQQ holdings changed — {as_of}`; the body has an `Added: ...` line and a
  `Removed: ...` line (a side with nothing is omitted), tickers comma-separated as
  `ZZZZ (Example New Co, 0.42%)` and `ALNY (Alnylam Pharmaceuticals Inc, was 0.10%)`.
  Overridable under the `qqqholdings_change` event in `configs/notify_config.json`
  (`title`, `message`) with placeholders `{as_of}`, `{previous_as_of}`, `{added_count}`,
  `{removed_count}` and `{changes}`.
- **Devices and switches:** sent through `notify_phone`, so it goes to the same devices as
  every other notification. A device opts out with `"disable": ["holdings_change"]` (a new
  group for the event). The master `notify_phone` job switch and the Home Assistant
  `notify` feature switch turn it off.
- **Best effort:** a failure is logged (log type `qqqholdings`) and shown in the run result
  as `"notification": {"status": ..., "message": ...}`; `run` still returns `status: "ok"`
  and nothing is retried. Nothing about notifying can raise out of `run`.
- **Ordering:** the notification is sent after the report and snapshot are saved and outside
  the run lock, so a slow Home Assistant (up to 30 s per device) can neither block another
  run nor undo the saved report.
- Implemented in `jobs/qqqholdings_notify.py`, called from `jobs/qqqholdings_job.py`.

## Testing

Same style as the existing `tests/`:

- Diff: added, removed, weight up, weight down, below threshold, identical.
- Parsing, against a saved sample response.
- Scheduler next-run calculation with an injected clock: weekend skip, already past
  6:30, timezone.
- Idempotence: a second run with the same as-of date writes nothing.

## Open items

- **Source: verified.** Invesco's `dng-api` returns HTTP 406 to the
  `shareclasses/QQQ` + `idType=ticker` form, and works with QQQ's CUSIP
  (`shareclasses/46090E103/holdings/fund?idType=cusip&productType=ETF`) and a
  non-browser User-Agent. Checked by hand on 2026-11-02: 106 rows, `effectiveBusinessDate`
  2026-10-30 (the last trading day).
- **Snapshots are keyed on `effectiveBusinessDate`**, not `effectiveDate`, which was a
  Sunday (2026-11-01).
- **Only stock lines count.** Cash, the quarterly-rolling index future and bookkeeping lines
  (some with a null ticker or null weight) are skipped; 101 stocks remain, summing to
  99.84%. Unknown security types are kept, so a new kind of holding is visible.
- **Fallback source:** TickerInside (`tickerinside.com/api/v1/fund/qqq.json`) republishes the
  same Invesco file with a lag (its holdings were a week old on 2026-11-02), no names or
  share counts, and a CC BY 4.0 attribution requirement. Not used.
- Timezone is Pacific (`America/Los_Angeles`), so 6:30 AM PT is 9:30 AM ET, the
  market open. The catch-up cutoff of 12:00 is also Pacific. Invesco's file will
  still carry the previous close's as-of date at that hour.
