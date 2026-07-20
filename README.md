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
    "leaving_home":  { "title": "Leaving home", "message": "The house is now armed.", "arm": true },
    "arriving_home": { "title": "Welcome home", "message": "The alarm has been disarmed.", "disarm": true }
}
```

- `arm` / `disarm` — when `true`, leaving also arms the panel and arriving also
  disarms it (reusing the same Home Assistant panel as `/webhook/blink/*`). Set
  to `false` to notify only.
- Each request may also override `title`, `message`, and the `arm`/`disarm` flag
  in its JSON body (payload wins over `configs/notify_config.json`, which wins over the
  built-in defaults).

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
| POST   | `/webhook/notify/leaving`     | Arm the panel (optional) + notify you're leaving home 🔒 |
| POST   | `/webhook/notify/arriving`    | Disarm the panel (optional) + notify you're arriving home 🔒 |
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
curl -X POST http://localhost:5050/webhook/notify/leaving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here"

curl -X POST http://localhost:5050/webhook/notify/arriving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"title": "Welcome back", "message": "Kettle is on"}'

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
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Webhooks with `"require_secret": true`, every state-changing management
endpoint (`/jobs/{name}/enable|disable|toggle` and
`/logs/{type}/enable|disable|toggle`), and reading log contents
(`/logs/{type}/read`) require the shared secret (from `configs/webhook_secret.json`) in
the `X-Webhook-Secret` header; requests without it get `401`. The remaining
read-only endpoints (`GET /jobs`, `GET /logs`, `/health`) are open. Use a
strong, random secret in production, and prefer a private network
(e.g. Tailscale) over public exposure.

## License

MIT
