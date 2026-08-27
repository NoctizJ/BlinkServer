# Blink Server

A Flask webhook server that arms/disarms an alarm panel through Home Assistant.
Its job system is modular, so new automations can be added without touching the
server code.

## Features

- Arm/disarm webhook endpoints backed by Home Assistant
- Single shared-secret authentication for webhooks
- Modular jobs — drop a new module in `jobs/` and register it in `configs/config.json`
- Enable/disable jobs at runtime via the API
- Structured, searchable logging with master + per-type switches — see [Logging.md](docs/Logging.md)
- File uploads (photos/videos/files) via a form-body webhook — see [Uploads.md](docs/Uploads.md)
- Per-person home/away presence, persisted in `state/presence.json` and readable
  over HTTP (`/webhook/presence/read`)
- Per-person location log — each id gets its own `state/<id>_loc.json`, written by
  `/webhook/location/log`, readable as JSON (ready for an iPhone Shortcut to open in
  Maps) or as a formatted text history, prunable by age, and able to notify your
  phone with a switch per person — see [Location log](#location-log)
- Leaving/arriving notification titles postfixed `(A)`/`(D)` for arm/disarm,
  depending on whether anyone is still home — see [Arm/disarm postfix](#armdisarm-postfix)

## Installation

```bash
git clone <repo-url> && cd BlinkServer
python3 -m venv venv && source venv/bin/activate
python3 -m pip install -r requirements.txt
```

Then create your Home Assistant config:

```bash
cp configs/home_assistant_config.example.json configs/home_assistant_config.json
```

Fill in your values:

```json
{
    "HA_BASE_URL": "http://localhost:8123",
    "HA_API_KEY": "your_home_assistant_long_lived_access_token",
    "HA_ENTITY_ID": "alarm_control_panel.blink_NAME"
}
```

> `configs/home_assistant_config.json` holds a secret token and is gitignored — never commit it.

`HA_NOTIFY_TARGET` (e.g. `mobile_app_aisingioro`) is only needed for the phone
notification webhooks (`/webhook/notify/*`); it names the Home Assistant
`notify` service target for your phone. The notification titles and messages are
configurable in **`configs/notify_config.json`**, which also controls whether each
event arms/disarms the alarm panel:

```json
{
    "leaving_home":  { "title": "Leaving home", "message": "{id} has left home.", "arm": true },
    "arriving_home": { "title": "Welcome home", "message": "{id} is home. The alarm has been disarmed.", "disarm": true }
}
```

- `arm` / `disarm` — when `true`, leaving also arms the panel and arriving also
  disarms it (reusing the same Home Assistant panel as `/webhook/blink/*`). Set
  to `false` to notify only.
- A third event, `location_log`, holds the text for the notification a logged
  position sends — see [Notifying your phone](#notifying-your-phone-per-person).
- `{id}` (or `{name}`) in a title/message is replaced with the person's name —
  see [Who left / arrived](#who-left--arrived) below.
- Every title also ends in an **arm/disarm postfix** — see
  [Arm/disarm postfix](#armdisarm-postfix).
- Each request may also override `title`, `message`, and the `arm`/`disarm` flag
  in its JSON body (payload wins over `configs/notify_config.json`, which wins over the
  built-in defaults).

### Arm/disarm postfix

Whenever a leaving or arriving webhook fires, the notification title gets a
postfix describing the household **after** that event:

| Postfix | Meaning | When                                      |
| ------- | ------- | ----------------------------------------- |
| `(A)`   | arm     | everyone's presence is `away`             |
| `(D)`   | disarm  | at least one person is still `home`       |

```text
Leaving home (D)     # somebody is still in the house
Leaving home (A)     # that was the last person out
Welcome home (D)     # arriving always leaves somebody home
```

The postfix is appended whatever the title's source (payload, config, or
built-in default), and is computed from `state/presence.json` with the current
event's own state applied on top — so the last person leaving gets `(A)` even
though the store is only written afterwards. It is a label for your automations
and notifications; it does not itself arm or disarm the panel (the `arm` /
`disarm` flags above do that). If the presence store cannot be read, the title
is sent without a postfix rather than failing the notification.

### Who left / arrived

`/webhook/notify/leaving` and `/webhook/notify/arriving` read the person's
identity from an **`id`** field in the JSON body:

```bash
curl -X POST http://localhost:5050/webhook/notify/leaving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex"}'
```

A post with no `id` (or a blank one) is attributed to **`娜`**.

Each event is persisted per person in **`state/presence.json`**, so the current
home/away state survives restarts. The file is created automatically and is
gitignored (it is runtime state):

```json
{
    "people": {
        "娜":  { "state": "home", "event": "arriving_home", "last_updated": "2026-08-03 18:42:11.482" },
        "Alex": { "state": "away", "event": "leaving_home",  "last_updated": "2026-08-03 08:07:53.119" }
    },
    "last_modified": "2026-08-03 18:42:11.482"
}
```

Leaving sets `"away"`, arriving sets `"home"`. Other jobs can read or write it
through `jobs/presence_state.py` (`resolve_person`, `get_state`, `all_states`,
`anyone_home`, `set_state`).

The store is also reachable over HTTP. Reading is a plain **GET** that returns
just the formatted text (like `/logs/{type}/read`); writing is a POST webhook.
Both need the secret header:

```bash
# Who's home, who's not
curl -H "X-Webhook-Secret: your-shared-secret-here" http://localhost:5050/presence
```

```text
Presence — 2 people
----------------------------------------------------------
Home (1): 娜
Away (1): Alex

Alex  away  since 2026-08-03 20:04:55.545  (leaving_home)
娜    home  since 2026-08-03 20:04:55.546  (arriving_home)
```

```bash
# One person
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?id=Alex"
# -> Alex is away since 2026-08-03 20:04:55.545 (leaving_home)

# Structured form, when you want the values rather than the text
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?format=json"
# -> {"status":"ok","count":2,"home":["娜"],"away":["Alex"],"people":{…},"message":"Presence — 2 people\n…"}

# The same reader as a POST webhook, for callers that prefer a JSON body
curl -X POST http://localhost:5050/webhook/presence/read \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex"}'

# Set a state by hand (seed the store, or fix a missed leaving/arriving webhook)
curl -X POST http://localhost:5050/webhook/presence/write \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "state": "home"}'
```

- `GET /presence` — plain text by default; `?id=<person>` for one person,
  `?format=json` for the structured payload. Also accepts POST with the same
  fields in a JSON body.
- `read` (the webhook) — no `id` returns everyone plus `home`/`away` name lists;
  an `id` returns just that person (`presence`/`state` are `null` if never seen).
  Every read includes `message`, the display-ready text.
- `write` — requires `state`; `id` defaults to `娜` and `event` defaults to
  `manual_write`. `state` accepts `home`/`in`/`true` and
  `away`/`left`/`out`/`not_home`/`false`.
- The two `/webhook/presence/*` paths are the single `presence_webhook` job, so
  `POST /jobs/presence_webhook/disable` turns the pair off. `GET /presence` is a
  management endpoint (like `/logs/{type}/read`) and is not affected by that
  switch.
- Nothing here arms or disarms the alarm panel — `presence/write` is bookkeeping
  only. The panel is changed by `/webhook/blink/*` and, when enabled in
  `configs/notify_config.json`, by `/webhook/notify/*`.

Then set the shared webhook secret:

```bash
cp configs/webhook_secret.example.json configs/webhook_secret.json
```

Put a long, random string in it:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
}
```

> `configs/webhook_secret.json` is gitignored — never commit it.

See [Home-Assistant-Setup.md](docs/Home-Assistant-Setup.md) for how to run Home
Assistant and generate a token.

## Running

```bash
source venv/bin/activate
python3 app.py            # runs on port 5050
python3 app.py --debug    # verbose logging

PORT=8080 python3 app.py  # custom port
```

The server binds to `0.0.0.0`. To reach it from other devices without exposing
ports, see [Tailscale-Setup.md](docs/Tailscale-Setup.md).

## API

| Method | Path                          | Description                        |
| ------ | ----------------------------- | ---------------------------------- |
| POST   | `/webhook/blink/arm`          | Arm the alarm panel                |
| POST   | `/webhook/blink/disarm`       | Disarm the alarm panel             |
| POST   | `/webhook/log`                | Write a log entry (see [Logging.md](docs/Logging.md)) |
| POST   | `/webhook/upload`             | Upload files, multipart/form-data (see [Uploads.md](docs/Uploads.md)) 🔒 |
| POST   | `/webhook/notify/leaving`     | Arm the panel (optional) + notify you're leaving home; `{"id": "<person>"}` 🔒 |
| POST   | `/webhook/notify/arriving`    | Disarm the panel (optional) + notify you're arriving home; `{"id": "<person>"}` 🔒 |
| POST   | `/webhook/presence/read`      | Read who's home / away, JSON body (see [Who left / arrived](#who-left--arrived)) 🔒 |
| POST   | `/webhook/presence/write`     | Set a person's home/away state by hand 🔒 |
| GET    | `/presence`                   | Read who's home / away as text; `?id=`, `?format=json` 🔒 |
| POST   | `/webhook/location/log`       | Log a location for a person (see [Location log](#location-log)) 🔒 |
| POST   | `/webhook/location/fetch`     | Read back a person's last location, JSON body 🔒 |
| POST   | `/webhook/location/history`   | Read a person's whole history, formatted as text 🔒 |
| POST   | `/webhook/location/purge`     | Trim history to `records` (default 10) or `days` 🔒 |
| GET    | `/location`                   | Read back a person's last location as JSON; `?id=`, `?n=` 🔒 |
| GET    | `/location/history`           | Read a person's location history as text; `?id=`, `?n=` 🔒 |
| GET    | `/location/notify`            | List each person's location-notification switch 🔒 |
| POST   | `/location/notify/{id}/enable`  | Notify this person's logged positions 🔒 |
| POST   | `/location/notify/{id}/disable` | Stop notifying for this person 🔒 |
| POST   | `/location/notify/{id}/toggle`  | Flip this person's notification switch 🔒 |
| GET    | `/jobs`                       | List jobs and their status         |
| POST   | `/jobs/{job_name}/enable`     | Enable a job 🔒                     |
| POST   | `/jobs/{job_name}/disable`    | Disable a job 🔒                    |
| POST   | `/jobs/{job_name}/toggle`     | Toggle a job on/off 🔒              |
| GET    | `/logs`                       | List log types and their status    |
| GET    | `/logs/{type}/read`           | Read recent log entries as text 🔒  |
| POST   | `/logs/{type}/enable`         | Enable a log type 🔒                |
| POST   | `/logs/{type}/disable`        | Disable a log type 🔒               |
| POST   | `/logs/{type}/toggle`         | Toggle a log type on/off 🔒         |
| GET    | `/health`                     | Health check                       |

🔒 endpoints always require the shared secret in the `X-Webhook-Secret` header.
Webhooks require it only when their `require_secret` is `true`. Read-only
endpoints (`GET /jobs`, `GET /logs`, `/health`) are open. See
[Security](#security).

### Examples

```bash
# Arm
curl -X POST http://localhost:5050/webhook/blink/arm \
  -H "X-Webhook-Secret: your-shared-secret-here"

# Disarm
curl -X POST http://localhost:5050/webhook/blink/disarm \
  -H "X-Webhook-Secret: your-shared-secret-here"

# Notify your phone (title/message default to configs/notify_config.json; override per request)
# "id" names who left/arrived and is recorded in state/presence.json (defaults to "娜")
curl -X POST http://localhost:5050/webhook/notify/leaving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex"}'

curl -X POST http://localhost:5050/webhook/notify/arriving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "title": "Welcome back", "message": "Kettle is on, {id}"}'

# Toggle a job or log type (secret required)
curl -X POST http://localhost:5050/jobs/log/toggle \
  -H "X-Webhook-Secret: your-shared-secret-here"

curl -X POST http://localhost:5050/logs/blink/disable \
  -H "X-Webhook-Secret: your-shared-secret-here"
```

## Location log

One job with four webhooks (`jobs/location_webhook.py`, the same shape as
`presence_webhook.py`) — the location counterpart of the `log` job: **`log`**
records where somebody is (and notifies your phone), **`fetch`** reads back the
latest position as JSON, **`history`** renders the whole history as text, and
**`purge`** trims it to the newest N records (or to a number of days). Nothing
goes into `logs/default.log` or any other text log — each person's positions live
in their own JSON file under `state/`.

Every one of them takes an **`id`** and defaults it the same way the presence
webhooks do: a missing, blank, or non-string `id` is attributed to **`娜`**.

```bash
# Log a position
curl -X POST http://localhost:5050/webhook/location/log \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "latitude": 37.334606, "longitude": -122.009102,
       "address": "Apple Park, Cupertino", "time": "2026-08-18 09:15:23.123"}'

# Read the latest one back
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location?id=Alex"
```

```json
{
    "status": "ok",
    "id": "Alex",
    "found": true,
    "latitude": 37.334606,
    "longitude": -122.009102,
    "address": "Apple Park, Cupertino",
    "time": "2026-08-18 09:15:23.123",
    "recorded_at": "2026-08-18 09:15:23.980",
    "trigger": "arrived home",
    "maps_url": "https://maps.apple.com/?ll=37.334606,-122.009102&q=Apple+Park%2C+Cupertino",
    "google_maps_url": "https://www.google.com/maps?q=37.334606,-122.009102",
    "message": "Alex was at Apple Park, Cupertino at 2026-08-18 09:15:23.123",
    "file": "Alex_loc.json"
}
```

### Logging a location (`log`)

| Field       | Required | Notes                                                        |
| ----------- | -------- | ------------------------------------------------------------ |
| `id`        | no       | Who this position belongs to; defaults to `娜`, same as presence |
| `latitude`  | **yes**  | −90…90; also accepts `lat`, and numeric strings               |
| `longitude` | **yes**  | −180…180; also accepts `lon` / `lng` / `long`                 |
| `address`   | no       | Free text; becomes the Maps pin label. Stored as `null` if omitted |
| `time`      | no       | The caller's own timestamp, stored **verbatim**; defaults to now |
| `trigger`   | no       | Why it was logged — "arrived home", "periodic", "manual". Alias `reason` |

A missing or out-of-range coordinate is reported as a JSON error and nothing is
written. Coordinates may arrive as strings, because Shortcuts sends every field
as text.

**`trigger`** is free text saying what caused the log, and once given it follows
the entry everywhere:

```bash
curl -X POST http://localhost:5050/webhook/location/log \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "latitude": 37.334606, "longitude": -122.009102,
       "address": "Apple Park", "trigger": "arrived home"}'
```

| Surface | With a trigger |
| --- | --- |
| the `log` response `message` | `Logged Alex at 37.334606,-122.009102 (Apple Park) at … ; trigger: arrived home` |
| the phone notification | `Alex is at Apple Park (…) — arrived home.` |
| `GET /location` | a `trigger` field, and `… at … (arrived home)` in `message` |
| `GET /location/history` | `Apple Park  [arrived home]` at the end of the row |
| `state/<id>_loc.json` | a `"trigger"` field on the entry |

Leave it out and every one of those reads exactly as it did before — no empty
brackets, no dangling dash. Blank strings count as "not given", and entries logged
before this field existed read back as `null`.

### Notifying your phone (per person)

Every logged position also pushes a notification to your phone, through the same
Home Assistant `notify` service the arrival/departure webhooks use. **Two
switches** gate it, and both must be on:

| Switch     | Where                                                     | Scope                                     |
| ---------- | --------------------------------------------------------- | ----------------------------------------- |
| master     | `configs/job_config.json` → `notify_phone`                 | every phone notification this server sends |
| per person | `configs/location_notify_config.json` → `ids.<id>`         | one person's logged positions              |

```bash
# Who is getting location notifications?
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify
```

```json
{
    "master": { "job": "notify_phone", "enabled": true },
    "ids": [ { "id": "Alex", "enabled": true }, { "id": "娜", "enabled": false } ]
}
```

```bash
# One person off, on, or flipped
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify/Alex/disable
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify/Alex/toggle

# Silence the lot — this is the notify_phone job, so it also covers
# /webhook/notify/leaving and /webhook/notify/arriving
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/jobs/notify_phone/disable
```

- A person nobody has toggled yet is **on**, and is written into
  `location_notify_config.json` the first time they log a position — so they show
  up in the listing and can be turned off. The file is created automatically:

  ```json
  {
      "ids": { "Alex": true, "娜": false },
      "last_modified": "2026-08-18 21:04:11.221"
  }
  ```

- The title and message live with every other notification's text, in
  **`configs/notify_config.json`** under `location_log`:

  ```json
  "location_log": {
      "title": "位置情報を記録",
      "message": "{id} is at {address} ({time}).",
      "message_with_trigger": "{id} is at {address} ({time}) — {trigger}."
  }
  ```

  `message_with_trigger` is used when the logged position carries a `trigger`, and
  `message` when it does not — that way neither version ends up with an empty
  clause. If you only set `message`, a triggered position falls back to it.

  which arrives as **位置情報を記録** / *Alex is at Apple Park, Cupertino
  (2026-08-18 09:15:23.123).* Only this event's title is Japanese —
  `leaving_home` and `arriving_home` keep their own text.

  Placeholders: `{id}`/`{name}`, `{address}`, `{latitude}`, `{longitude}`,
  `{time}`, `{trigger}` and `{maps_url}`. `{address}` falls back to the coordinates when the
  logged position had none, and `{time}` is the entry's own timestamp — the one
  the caller sent, or the moment the server stored it. A request may override `title`/`message` per call, so
  precedence matches the notify webhooks: payload > `notify_config.json` >
  built-in default.
- Switched off means "do not notify", **not** "do not log" — the position is
  still stored either way.
- The outcome comes back in the `notify` field of the `/webhook/location/log`
  response (`success`, `skipped` with the reason, or `error`). A notification that
  cannot be sent never fails the write.
- Non-ASCII ids work in the path: `/location/notify/娜/disable`.

### The store

Each id gets **`state/<id>_loc.json`** (created automatically, gitignored as
runtime state):

```json
{
    "id": "Alex",
    "entries": [
        {
            "latitude": 37.334606,
            "longitude": -122.009102,
            "address": "Apple Park, Cupertino",
            "time": "2026-08-18 09:15:23.123",
            "recorded_at": "2026-08-18 09:15:23.980",
            "trigger": "arrived home"
        }
    ],
    "last_modified": "2026-08-18 09:15:23.980"
}
```

- `time` is yours; `recorded_at` is when this server wrote the entry.
- Entries **append**, newest last, capped at the newest 500 per person
  (`MAX_ENTRIES` in `jobs/location_state.py`) so a chatty phone cannot grow the
  file without bound. "Latest" therefore means most recently *logged*, not the
  largest `time`. Trim by count or age with [`purge`](#purging-old-entries-purge).
- The filename is sanitized: path separators, control characters and leading dots
  in an `id` can never write outside `state/`. Non-ASCII names (`娜_loc.json`)
  are kept as-is.
- Other jobs can read or write the store through `jobs/location_state.py`
  (`append_location`, `latest_location`, `location_entries`, `prune_locations`).
  Reading a caller's payload and building the map links live one level up, in
  `jobs/location_webhook.py`.

### Reading it back (`fetch`)

```bash
# One person, plus their 5 most recent positions
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location?id=Alex&n=5"

# The same reader as a POST webhook, for callers that prefer a JSON body
curl -X POST http://localhost:5050/webhook/location/fetch \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex"}'
```

- `GET /location` — JSON; `?id=<person>` picks whom (defaults to `娜`), `?n=<count>`
  adds an `entries` list of recent positions, newest first. Also accepts POST with
  the same fields in a JSON body.
- Two ready-to-open map links come with every found position — **`maps_url`** for
  Apple Maps (`maps.apple.com/?ll=<lat>,<lon>&q=<address>`, pin labelled with the
  address) and **`google_maps_url`** for Google Maps
  (`google.com/maps?q=<lat>,<lon>`, works on Android and in any browser). Both are
  `null` if the stored entry has no usable coordinates.
- An id with nothing logged yet is **not** an error: you get a normal `200` with
  `"found": false` and null fields, so a Shortcut does not fail on a `404`.
- The four `/webhook/location/*` paths are the single `location_webhook` job, so
  `POST /jobs/location_webhook/disable` turns the set off — exactly as
  `presence_webhook` covers both presence paths. Like `GET /presence`, the two
  `GET /location*` endpoints are management endpoints and are not affected by that
  switch (the `/webhook/location/*` paths are).

### The whole history as text (`history`)

`fetch` only answers "where are they now". For "where have they been", read
the history — this one is **plain text**, not JSON, like `/logs/{type}/read`:

```bash
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location/history?id=Alex"
```

```text
Location history — Alex — 3 entries (newest first)
------------------------------------------------------------------------
2026-08-18 09:15:23.123    37.334606, -122.009102   Apple Park, Cupertino
2026-08-17 20:04:55.545    51.501400,   -0.141900   Buckingham Palace
2026-08-16 08:07:53.119    37.331800, -122.031200   -
```

- Everything is returned by default; `?n=<count>` caps it at the most recent
  `count` entries and the header then reads `2 of 340 entries`.
- Rows are **newest first**, in the order they were logged — the store is never
  re-sorted by the `time` column, so an entry sent with a wrong clock stays where
  it was logged.
- The column shown is each entry's own `time`. An entry with no address shows `-`;
  a hand-edited entry missing a coordinate shows `?` rather than breaking the
  table.
- `POST /webhook/location/history` returns the same text in the `message` field, for
  callers that want a JSON body.

### Purging old entries (`purge`)

Two ways to say what to keep — **by count** (the default) or **by age**:

```bash
# Keep Alex's 25 most recent positions, delete the rest
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "records": 25}'

# Keep the last 30 days instead
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "days": 30}'

# Neither: keeps the 10 most recent
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "X-Webhook-Secret: your-shared-secret-here"
```

```json
{
    "status": "ok",
    "id": "Alex",
    "mode": "records",
    "records": 25,
    "days": null,
    "cutoff": null,
    "removed": 5,
    "kept": 25,
    "undated": 0,
    "message": "Purged 5 entries beyond the 25 most recent for Alex; 25 kept."
}
```

| Input                  | Keeps                                  | Mode      |
| ---------------------- | -------------------------------------- | --------- |
| `{"records": 25}`      | the 25 most recently logged entries    | `records` |
| `{"days": 30}`         | everything logged in the last 30 days  | `days`    |
| both                   | **records wins**, `days` is ignored    | `records` |
| neither                | the 10 most recent (`DEFAULT_RECORDS`) | `records` |

- **`records` beats `days` whenever both are passed.** The response echoes both
  inputs and `mode` tells you which rule was applied — in records mode `cutoff` is
  `null` and the message ends `(days ignored)`. Check `records`/`days` in the
  response to confirm the server read what you sent.
- Either field also works as a query parameter —
  `POST /webhook/location/purge?id=Alex&records=3` — see
  [Configuration](#configuration).
- `records` may be a numeric string and must be a whole number ≥ 0; `records: 0`
  empties the history (the file stays). `keep` works as an alias.
- `days` may be any number ≥ 0 — `nan`, `inf` and negatives are refused. `days: 0`
  means "everything up to now". It has no default: leave it out and the record
  count applies instead.
- Counting by records is **positional** — it ignores timestamps entirely, so it
  works even on entries whose `time` is unparseable.
- Ageing by days uses **`recorded_at`**, the timestamp this server wrote, falling
  back to the caller's `time` only if `recorded_at` is missing. A phone with a
  wrong clock therefore cannot talk the server into deleting fresh data.
- In days mode, an entry whose timestamp cannot be parsed at all is **kept** and
  counted in `undated` — deleting data the server cannot date would be worse than
  keeping it.
- It only ever touches the one id's file. There is no GET form: purging is
  destructive, so it is POST-with-the-secret only.
- To run it on a schedule, point cron (or a Home Assistant automation) at the
  webhook:

  ```bash
  # 04:00 daily, keep the last 30 days
  0 4 * * * curl -sS -X POST http://localhost:5050/webhook/location/purge \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex", "days": 30}'
  ```

### Opening it in Maps from an iPhone Shortcut

The response's `maps_url` is a ready-to-open Apple Maps link, so the Shortcut is
three actions:

1. **Get Contents of URL** — `https://<your-server>/location?id=Alex`, method
   `GET`, with header `X-Webhook-Secret: your-shared-secret-here`.
2. **Get Dictionary Value** — key `maps_url` (or `google_maps_url` to open Google
   Maps instead).
3. **Open URLs** — Maps opens on the pin, labelled with the address.

To log a position from the phone instead, use **Get Current Location** and POST
its `Latitude` / `Longitude` to `/webhook/location/log`.

## Configuration

**`configs/config.json`** maps webhook paths to job modules:

```json
{
    "webhooks": [
        {
            "path": "/webhook/blink/arm",
            "module": "jobs.home_assistant_arm_disarm",
            "require_secret": true
        }
    ]
}
```

- `module` — the job module that handles the request (must expose a `run(payload)` function)
- `function` — the function to call, when a module handles several webhooks (defaults to `run`)
- `require_secret` — when `true`, the request must include the shared secret in the `X-Webhook-Secret` header; `false` disables auth for that webhook

A webhook's inputs may be sent **either as a JSON body or as query parameters**,
and the body wins when a field appears in both:

```bash
curl -X POST "http://localhost:5050/webhook/location/purge?id=Alex&records=3" \
  -H "X-Webhook-Secret: your-shared-secret-here"

curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex", "records": 3}'
```

A JSON body is parsed even if you forget `Content-Type: application/json` —
otherwise it would be dropped silently and the job would run with its defaults.
The one exception is `/webhook/upload`, whose multipart body belongs to the job
itself (see [Uploads.md](docs/Uploads.md)); send its fields as query parameters or
form fields.

**`configs/webhook_secret.json`** holds the single shared secret used by every
authenticated webhook. It is gitignored — copy it from the example and fill it in:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
}
```

**`configs/location_notify_config.json`** holds one on/off switch per person for
the notification a logged position sends (see
[Notifying your phone](#notifying-your-phone-per-person)). It is created
automatically and updated through the `/location/notify` endpoints:

```json
{
    "ids": { "Alex": true }
}
```

**`configs/job_config.json`** tracks which jobs are enabled. It is created automatically
and updated through the `/jobs` endpoints — you rarely edit it by hand:

```json
{
    "jobs": {
        "home_assistant_arm_disarm": true
    }
}
```

## Testing

```bash
python3 jobs/home_assistant_arm_disarm.py   # exercise the job directly
python3 tests/test_job_management.py        # job enable/disable logic
python3 tests/test_log_engine.py            # logging engine tests
python3 tests/test_file_upload.py           # file upload job tests
python3 tests/test_notify_phone.py          # phone notification job tests
python3 tests/test_presence_webhook.py      # presence read/write webhook tests
python3 tests/test_location.py              # location log/fetch/history/purge + notify tests
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Webhooks with `"require_secret": true`, every state-changing management
endpoint (`/jobs/{name}/enable|disable|toggle` and
`/logs/{type}/enable|disable|toggle`), reading log contents
(`/logs/{type}/read`), reading presence (`GET /presence`), reading a logged
location (`GET /location`, `GET /location/history`) and the location-notification
switches (`GET /location/notify`, `/location/notify/{id}/enable|disable|toggle`)
require the shared
secret (from `configs/webhook_secret.json`) in
the `X-Webhook-Secret` header; requests without it get `401`. The remaining
read-only endpoints (`GET /jobs`, `GET /logs`, `/health`) are open. Use a
strong, random secret in production, and prefer a private network
(e.g. Tailscale) over public exposure.

## License

MIT
