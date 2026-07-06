# Automation Webhook Server

A generic Flask server for triggering automation jobs remotely via webhooks.
`app.py` never needs to change — it just reads `config.json` and dynamically
calls whatever job function you point it at.

## Structure

- `app.py` — generic server. Loads `config.json`, registers a POST route per
  webhook entry, dynamically imports the configured module and calls its
  `run(payload)` function.
- `config.json` — declares webhooks: path, target module/function, optional
  secret.
- `jobs/sample_job.py` — example job. `run(payload)` logs the trigger and
  payload and returns a JSON-serializable result.
- `requirements.txt` — dependencies (just Flask).

## Adding a new automation

1. Create a new module under `jobs/` with a `run(payload)` function.
2. Add an entry to `config.json`:
   ```json
   {
     "path": "/webhook/my-job",
     "module": "jobs.my_job",
     "function": "run",
     "secret": "optional-shared-secret"
   }
   ```
3. Restart the server. No changes to `app.py` needed.

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Server listens on `0.0.0.0:5000` by default. Override with `PORT`:

```bash
PORT=5050 ./venv/bin/python app.py
```

> **macOS note:** port 5000 is often taken by AirPlay Receiver. Either use a
> different `PORT`, or disable it in System Settings → General → AirDrop &
> Handoff → AirPlay Receiver.

## Usage

```bash
curl -X POST http://127.0.0.1:5050/webhook/sample \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'
```

(replace `5050` with `5000` if you didn't override `PORT`)

Health check: `GET /health`

## Auth

If a webhook entry has a non-null `secret`, requests must include it in the
`X-Webhook-Secret` header, or the server responds `401 Unauthorized`.

## Remote access

To reach this server from another device (phone, other machine) with a
stable address, see [TAILSCALE_SETUP.md](TAILSCALE_SETUP.md).

## Status / open items

- Verified via Flask's in-process test client (routing, dynamic job dispatch,
  404 on unknown routes, secret-header auth). Not yet tested over a real
  HTTP socket — do that first when you run it locally.
- Remote access model (LAN/VPN vs. public internet) and job model (fixed
  named jobs vs. arbitrary commands) are still undecided — current setup
  assumes local network and named jobs only.
