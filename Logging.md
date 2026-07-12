# Logging System

A small logging engine (`jobs/log_engine.py`) that appends pretty, searchable,
timestamped entries to files under `logs/`. Every entry has a *type*, and two
switches decide whether it is actually written.

## Where entries go

Entries are routed by type, each type to its own file:

| Type            | File                |
| --------------- | ------------------- |
| `blink`         | `logs/blink.log`    |
| `upload`        | `logs/upload.log`   |
| everything else | `logs/default.log`  |

## Log format

Entries are separated by a rule, with the timestamp and type on one line so the
files are easy to grep:

```
================================================================================
[2026-07-11 09:20:08.083] [BLINK]
--------------------------------------------------------------------------------
Camera 1 detected motion in the driveway.
```

Search examples:

```bash
grep "\[BLINK\]" logs/blink.log       # all entries of a type
grep "motion" logs/*.log              # by message text, across files
```

> `logs/*.log` is gitignored — logs stay local.

## Logging from code

```python
from jobs.log_engine import log

log("blink", "Camera 1 detected motion")   # typed entry
log(None, "Something happened")             # type omitted -> "default"
```

`log(log_type, text)` returns `True` when written and `False` when suppressed by
a switch.

## Logging via webhook

`POST /webhook/log` writes a log entry from a JSON payload. `type` is optional
(nullable); `text` is required.

```bash
# Typed entry
curl -X POST http://localhost:5050/webhook/log \
  -H "Content-Type: application/json" \
  -d '{"type": "blink", "text": "Front door camera motion"}'

# No type -> default
curl -X POST http://localhost:5050/webhook/log \
  -H "Content-Type: application/json" \
  -d '{"text": "no type provided"}'
```

The response includes `written: true|false`.

## How the switches work

A line is written only when **both** switches are on:

1. **Master switch** — the `log` job in `job_config.json`. Turns *all* logging on/off.
2. **Per-type switch** — an entry under `types` in `log_config.json`. Turns a single type on/off.

| Master (`log`) | Type | Result     |
| -------------- | ---- | ---------- |
| on             | on   | written    |
| on             | off  | suppressed |
| off            | any  | suppressed |

New types are auto-registered in `log_config.json` (enabled) the first time they
are logged, so you can toggle them off afterwards.

### Config files

`job_config.json` — the master switch lives beside the other jobs:

```json
{
  "jobs": {
    "log": true
  }
}
```

`log_config.json` — one flag per type:

```json
{
  "types": {
    "default": true,
    "blink": true,
    "upload": true
  }
}
```

## Managing switches at runtime

The master switch (the `log` job) uses the generic job endpoints:

| Method | Path                | Description         |
| ------ | ------------------- | ------------------- |
| POST   | `/jobs/log/enable`  | Turn all logging on |
| POST   | `/jobs/log/disable` | Turn all logging off|
| POST   | `/jobs/log/toggle`  | Toggle all logging  |

Per-type switches:

| Method | Path                   | Description                 |
| ------ | ---------------------- | --------------------------- |
| GET    | `/logs`                | List types and their status |
| POST   | `/logs/{type}/enable`  | Enable a type               |
| POST   | `/logs/{type}/disable` | Disable a type              |
| POST   | `/logs/{type}/toggle`  | Toggle a type               |

```bash
curl http://localhost:5050/logs
curl -X POST http://localhost:5050/logs/blink/disable
```

## Reading logs

`GET /logs/{type}/read` returns recent log entries as **plain text** — handy for
reading from a phone. It **requires the shared secret** (`X-Webhook-Secret`
header). Routing matches writes: `blink` reads `logs/blink.log`, everything else
reads `logs/default.log`.

| Query | Meaning                                             |
| ----- | --------------------------------------------------- |
| `?n=N`| Return the most recent `N` entries (default `20`)   |
| `?n=0`| Return the whole file                               |

```bash
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/logs/blink/read?n=30"
```

### From an iPhone Shortcut

1. **Get Contents of URL** → `http://<server>:5050/logs/blink/read?n=30`
2. **Method**: `GET` (POST also works).
3. **Headers**: `X-Webhook-Secret` = your secret.
4. Add **Show Result** (or **Quick Look**) on the *Contents of URL* — the log
   text is displayed directly.

## Arm/disarm events

The Home Assistant arm/disarm job logs every event (success, failure, and
errors) under the `blink` type, giving you an audit trail in `logs/blink.log`.

## Testing

```bash
python3 test_log_engine.py     # log engine unit tests (no server needed)
python3 jobs/log_engine.py     # write a few sample entries
python3 jobs/log_webhook.py    # exercise the webhook handler directly
```
