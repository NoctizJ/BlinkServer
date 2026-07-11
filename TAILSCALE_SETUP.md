# Remote Access via Tailscale

Tailscale gives the server a stable private IP you can reach from anywhere
(phone, another laptop) without opening router ports or exposing it publicly.

It creates a private mesh network ("tailnet") between your devices. Each device
gets a fixed `100.x.y.z` IP that never changes, and all traffic is end-to-end
encrypted (WireGuard).

## 1. Install on this laptop

```bash
brew install --cask tailscale
```

Open **Tailscale** from Applications, click **Log In**, and sign in with any
provider (Google, GitHub, etc. — just used to identify your account). When
prompted, approve the system extension under **System Settings → General →
Login Items & Extensions**.

## 2. Get this laptop's Tailscale IP

```bash
tailscale ip -4
```

This prints a stable `100.x.y.z` address that survives WiFi changes and reboots.

## 3. Install on the client device

Install Tailscale on your phone (or any other device) and log in with the
**same account** — it joins the tailnet automatically.

## 4. Start the server

```bash
cd ~/Documents/BlinkServer
PORT=5050 ./venv/bin/python3 app.py
```

The server binds to `0.0.0.0`, so it's reachable over Tailscale with no code changes.

## 5. Send a request from the other device

```bash
curl -X POST http://100.x.y.z:5050/webhook/sample \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'
```

Use the IP from step 2.

## Notes

- Only devices on your tailnet can reach this address — it isn't publicly
  routable. Still, keep a `secret` on any webhook that triggers something
  sensitive.
- For a name instead of an IP, enable **MagicDNS** in the
  [admin console](https://login.tailscale.com/admin/dns), then use something
  like `http://your-laptop-name:5050`.
- Check status any time with `tailscale status`.
