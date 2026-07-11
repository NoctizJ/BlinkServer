# Blink Server

A Flask webhook server that arms/disarms an alarm panel through Home Assistant.
Its job system is modular, so new automations can be added without touching the
server code.

## Features

- Arm/disarm webhook endpoints backed by Home Assistant
- Single shared-secret authentication for webhooks
- Modular jobs — drop a new module in `jobs/` and register it in `config.json`
- Enable/disable jobs at runtime via the API
- Structured, searchable logging with master + per-type switches — see [Logging.md](Logging.md)

## Installation

```bash
git clone <repo-url> && cd BlinkServer
python3 -m venv venv && source venv/bin/activate
python3 -m pip install -r requirements.txt
```

Then create your Home Assistant config:

```bash
cp home_assistant_config.example.json home_assistant_config.json
```

Fill in your values:

```json
{
    "HA_BASE_URL": "http://localhost:8123",
    "HA_API_KEY": "your_home_assistant_long_lived_access_token",
    "HA_ENTITY_ID": "alarm_control_panel.blink_NAME"
}
```

> `home_assistant_config.json` holds a secret token and is gitignored — never commit it.

Then set the shared webhook secret:

```bash
cp webhook_secret.example.json webhook_secret.json
```

Put a long, random string in it:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
}
```

> `webhook_secret.json` is gitignored — never commit it.

See [Home Assistant Setup.md](Home%20Assistant%20Setup.md) for how to run Home
Assistant and generate a token.

## Running

```bash
source venv/bin/activate
python3 app.py            # runs on port 5050
python3 app.py --debug    # verbose logging

PORT=8080 python3 app.py  # custom port
```

The server binds to `0.0.0.0`. To reach it from other devices without exposing
ports, see [TAILSCALE_SETUP.md](TAILSCALE_SETUP.md).

## API

| Method | Path                          | Description                     |
| ------ | ----------------------------- | ------------------------------- |
| POST   | `/webhook/blink/arm`          | Arm the alarm panel             |
| POST   | `/webhook/blink/disarm`       | Disarm the alarm panel          |
| POST   | `/webhook/log`                | Write a log entry (see [Logging.md](Logging.md)) |
| GET    | `/jobs`                       | List jobs and their status      |
| POST   | `/jobs/{job_name}/enable`     | Enable a job                    |
| POST   | `/jobs/{job_name}/disable`    | Disable a job                   |
| POST   | `/jobs/{job_name}/toggle`     | Toggle a job on/off             |
| GET    | `/logs`                       | List log types and their status |
| POST   | `/logs/{type}/enable`         | Enable a log type               |
| POST   | `/logs/{type}/disable`        | Disable a log type              |
| POST   | `/logs/{type}/toggle`         | Toggle a log type on/off        |
| GET    | `/health`                     | Health check                    |

Webhook requests must include the matching secret in the `X-Webhook-Secret`
header (see [Security](#security)).

### Examples

```bash
# Arm
curl -X POST http://localhost:5050/webhook/blink/arm \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"action": "arm"}'

# Disarm
curl -X POST http://localhost:5050/webhook/blink/disarm \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"action": "disarm"}'
```

## Configuration

**`config.json`** maps webhook paths to job modules:

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

**`webhook_secret.json`** holds the single shared secret used by every
authenticated webhook. It is gitignored — copy it from the example and fill it in:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
}
```

**`job_config.json`** tracks which jobs are enabled. It is created automatically
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
python3 test_job_management.py              # job enable/disable logic
python3 test_log_engine.py                  # logging engine tests
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Webhooks with `"require_secret": true` require the shared secret (from
`webhook_secret.json`) in the `X-Webhook-Secret` header; requests without it
get `401`. Use a strong, random secret in production, and prefer a private
network (e.g. Tailscale) over public exposure.

## License

MIT
