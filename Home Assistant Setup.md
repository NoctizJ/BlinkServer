# Home Assistant Setup for Blink

How to run Home Assistant locally and generate an access token for Blink Server.

## 1. Run Home Assistant in Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and
verify it's ready:

```bash
docker --version
```

Start Home Assistant:

```bash
mkdir -p ~/homeassistant/config
docker run -d \
  --name homeassistant \
  --restart unless-stopped \
  -p 8123:8123 \
  -v $HOME/homeassistant/config:/config \
  -e TZ=America/Los_Angeles \
  ghcr.io/home-assistant/home-assistant:stable

docker ps   # confirm the container is running
```

## 2. Configure Home Assistant

1. Open <http://localhost:8123> and create your account.
2. **Settings → Devices & services → + Add Integration → Blink**.
3. Generate a token: **Profile → Security → Long-lived access tokens**.

Put the token and your entity ID into `home_assistant_config.json` (see the
main [README](README.md)).

## 3. Verify with curl

Replace the placeholders with your host and token.

```bash
# Arm
curl -X POST \
  http://<YOUR_HOME_ASSISTANT_HOST>:8123/api/services/alarm_control_panel/alarm_arm_away \
  -H "Authorization: Bearer <YOUR_LONG_LIVED_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "alarm_control_panel.blink_armstrong"}'

# Disarm
curl -X POST \
  http://<YOUR_HOME_ASSISTANT_HOST>:8123/api/services/alarm_control_panel/alarm_disarm \
  -H "Authorization: Bearer <YOUR_LONG_LIVED_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "alarm_control_panel.blink_armstrong"}'
```
