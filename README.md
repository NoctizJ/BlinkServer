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
- `{id}` (or `{name}`) in a title/message is replaced with the person's name —
  see [Who left / arrived](#who-left--arrived) below.
- Each request may also override `title`, `message`, and the `arm`/`disarm` flag
  in its JSON body (payload wins over `configs/notify_config.json`, which wins over the
  built-in defaults).

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
`set_state`).

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
- `require_secret` — when `true`, the request must include the shared secret in the `X-Webhook-Secret` header; `false` disables auth for that webhook

**`configs/webhook_secret.json`** holds the single shared secret used by every
authenticated webhook. It is gitignored — copy it from the example and fill it in:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
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
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Webhooks with `"require_secret": true`, every state-changing management
endpoint (`/jobs/{name}/enable|disable|toggle` and
`/logs/{type}/enable|disable|toggle`), reading log contents
(`/logs/{type}/read`), and reading presence (`GET /presence`) require the shared
secret (from `configs/webhook_secret.json`) in
the `X-Webhook-Secret` header; requests without it get `401`. The remaining
read-only endpoints (`GET /jobs`, `GET /logs`, `/health`) are open. Use a
strong, random secret in production, and prefer a private network
(e.g. Tailscale) over public exposure.

## License

MIT
