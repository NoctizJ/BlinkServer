# Remote Access via Tailscale

This gives the automation server a stable private IP address that you can
reach from anywhere (phone, another laptop, etc.) without opening any ports
on your router or exposing it to the public internet.

## How it works

Tailscale creates a private mesh network (a "tailnet") between your devices.
Each device gets a fixed IP in the `100.x.y.z` range that never changes,
regardless of your home network or ISP. Traffic between devices is
end-to-end encrypted (WireGuard) and never touches the public internet.

## 1. Install Tailscale on this laptop

```bash
brew install --cask tailscale
```

Open **Tailscale** from Applications, click **Log In**, and sign in with
any identity provider (Google, GitHub, Microsoft, Apple — just used to
identify your account, doesn't matter which).

macOS will ask you to approve a system extension:
**System Settings → General → Login Items & Extensions** → approve
"Tailscale".

## 2. Get this laptop's Tailscale IP

```bash
tailscale ip -4
```

This prints something like `100.101.102.103`. This IP is stable — it
won't change when you reconnect to WiFi, change networks, or reboot.

## 3. Install Tailscale on the client device

On your phone or any other machine you want to send requests from,
install the Tailscale app and log in with the **same account**. It joins
the same tailnet automatically.

## 4. Start the automation server

```bash
cd ~/Documents/automation-server
PORT=5050 ./venv/bin/python3 app.py
```

The server already binds to `0.0.0.0`, so it's reachable on the Tailscale
interface with no code changes.

## 5. Send a request from the other device

```bash
curl -X POST http://100.101.102.103:5050/webhook/sample \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'
```

Replace `100.101.102.103` with the IP from step 2.

## Notes

- Only devices logged into your tailnet can reach this address — it's not
  publicly routable, so this is safe even without additional auth. That
  said, keep using the `secret` field in `config.json` for webhooks that
  trigger anything sensitive.
- If you want a name instead of an IP, enable **MagicDNS** in the
  [Tailscale admin console](https://login.tailscale.com/admin/dns) — then
  you can reach the laptop at something like `http://your-laptop-name:5050`.
- To check tailnet status any time: `tailscale status`.
