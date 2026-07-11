# Blink Server

A Flask-based server that provides webhook endpoints for automating alarm systems via Home Assistant.

## Features

- Webhook endpoints for arm/disarm alarm systems
- Secure authentication via shared secrets
- Modular job system with easy extensibility
- Logging and error handling
- Dynamic job enable/disable functionality
- Home Assistant integration for alarm panel control

## Installation

1. Clone the repository
2. Activate virtual environment:
   ```bash
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   python3 -m pip install -r requirements.txt
   ```

4. Set up Home Assistant integration:
   
   Create a `home_assistant_config.json` file in the root directory with your Home Assistant settings:
   ```json
   {
       "HA_BASE_URL": "http://localhost:8123",
       "HA_API_KEY": "your_home_assistant_long_lived_access_token", 
       "HA_ENTITY_ID": "alarm_control_panel.blink_armstrong"
   }
   ```

   **Note:** The `webhook_secret` is used to authenticate webhook requests. Make sure to use a strong, random secret value in production.

## Usage Notes

All commands should be run **within the activated virtual environment**:

```bash
source venv/bin/activate
python3 app.py --debug
```

## Webhook Endpoints

### Arm System
```
POST /webhook/blink/arm
```

### Disarm System
```
POST /webhook/blink/disarm
```

## Job Management

The server includes a job management system that allows you to enable/disable jobs dynamically:

- `GET /jobs` - List all jobs and their status
- `POST /jobs/{job_name}/enable` - Enable a specific job  
- `POST /jobs/{job_name}/disable` - Disable a specific job
- `POST /jobs/{job_name}/toggle` - Toggle a job's enabled/disabled status

## Configuration

The server is configured via `config.json` which defines webhook endpoints and their associated jobs.

### Webhook Endpoints

Each webhook endpoint requires a configuration in `config.json`:

```json
{
    "webhooks": [
        {
            "path": "/webhook/blink/arm",
            "module": "jobs.home_assistant_arm_disarm",
            "secret": "your-webhook-secret-here"
        },
        {
            "path": "/webhook/blink/disarm", 
            "module": "jobs.home_assistant_arm_disarm",
            "secret": "your-webhook-secret-here"
        }
    ]
}
```

The `module` field points to the Python module that handles the webhook request. This implementation only supports:
- `jobs.home_assistant_arm_disarm` (Home Assistant implementation)

### Job Management

Jobs are defined in `job_config.json` and can be enabled/disabled dynamically through API endpoints:

```json
{
    "jobs": {
        "home_assistant_arm_disarm": {
            "enabled": true
        }
    }
}
```

## Usage Examples

### Arm the system:
```bash
curl -X POST http://localhost:5050/webhook/blink/arm \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"action": "arm"}'
```

### Disarm the system:
```bash
curl -X POST http://localhost:5050/webhook/blink/disarm \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"action": "disarm"}'
```

## Port Configuration

By default, the server runs on port 5050. If you need to use a different port, set the `PORT` environment variable:

```bash
PORT=8080 python3 app.py --debug
```

## Testing

Run the setup test to verify configuration:
```bash
python3 test_blink_setup.py
```

For local testing of the arm/disarm functionality, you can run:

### 1. Direct function test (simulated):
```bash
python3 jobs/blink_arm_disarm.py
```

### 2. Server with debug mode for webhook testing:
```bash
# Start server in debug mode
python3 app.py --debug

# Then test with curl commands from the Usage Examples section
```

### 3. Full end-to-end testing:
- Start the server: `python3 app.py`
- Test arm endpoint: `curl -X POST http://localhost:5050/webhook/blink/arm ...`
- Test disarm endpoint: `curl -X POST http://localhost:5050/webhook/blink/disarm ...`

## Security

All webhook endpoints require authentication via the `X-Webhook-Secret` header. Make sure to use a strong secret value in production.

## License

MIT