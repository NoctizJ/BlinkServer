# Blink Server

A Flask webhook server that arms/disarms an alarm panel through Home Assistant.
Its job system is modular, so new automations can be added without touching the
server code.

## Features

- Arm/disarm webhook endpoints backed by Home Assistant
- Shared-secret authentication per webhook
- Modular jobs — drop a new module in `jobs/` and register it in `config.json`
- Enable/disable jobs at runtime via the API

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
    "HA_ENTITY_ID": "alarm_control_panel.blink_armstrong"
}
```

> `home_assistant_config.json` holds a secret token and is gitignored — never commit it.

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
| GET    | `/jobs`                       | List jobs and their status      |
| POST   | `/jobs/{job_name}/enable`     | Enable a job                    |
| POST   | `/jobs/{job_name}/disable`    | Disable a job                   |
| POST   | `/jobs/{job_name}/toggle`     | Toggle a job on/off             |
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

**`config.json`** maps webhook paths to job modules and their secrets:

```json
{
    "webhooks": [
        {
            "path": "/webhook/blink/arm",
            "module": "jobs.home_assistant_arm_disarm",
            "secret": "your-shared-secret-here"
        }
    ]
}
```

- `module` — the job module that handles the request (must expose a `run(payload)` function)
- `secret` — required in the `X-Webhook-Secret` header; use `null` to disable auth

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
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Every webhook with a `secret` requires that value in the `X-Webhook-Secret`
header; requests without it get `401`. Use a strong, random secret in
production, and prefer a private network (e.g. Tailscale) over public exposure.

## License

MIT
