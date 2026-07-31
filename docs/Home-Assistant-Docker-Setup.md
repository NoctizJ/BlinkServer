# Home Assistant Setup for Blink

How to run Home Assistant locally using Docker Compose and generate a long-lived access token for Blink Server.

## 1. Install Docker Desktop

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and verify it's running:

```bash
docker --version
docker compose version
```

## 2. Create the Home Assistant Directory

Create a working directory for Home Assistant:

```bash
mkdir -p ~/homeassistant/config
mkdir -p ~/homeassistant/media

cd ~/homeassistant
```

## 3. Create `docker-compose.yml`

Create a file named `docker-compose.yml`:

```yaml
services:
  homeassistant:
    container_name: homeassistant
    image: ghcr.io/home-assistant/home-assistant:stable
    restart: unless-stopped

    ports:
      - "8123:8123"

    environment:
      TZ: America/Los_Angeles

    volumes:
      - ./config:/config
      - ./media:/media
```

## 4. Start Home Assistant

Run:

```bash
docker compose up -d
```

Verify that Home Assistant is running:

```bash
docker compose ps
```

To stop Home Assistant:

```bash
docker compose down
```

You should see the `homeassistant` container with the status `Up`.

## 5. Configure Home Assistant

1. Open <http://localhost:8123>.
2. Complete the initial setup and create your account.
3. Go to **Settings → Devices & Services → Add Integration → Blink**.
4. Generate a long-lived access token:

   **Profile → Security → Long-lived access tokens**

Update `configs/home_assistant_config.json` with your token and Blink entity ID (see the main [README](../README.md)).

## 6. Upgrade Home Assistant

One advantage of Docker Compose is that upgrading Home Assistant only requires two commands.

Pull the latest image:

```bash
docker compose pull
```

Restart using the new image:

```bash
docker compose up -d
```

(Optional) Remove old, unused images:

```bash
docker image prune -f
```

## 7. Verify with curl

Replace the placeholders with your Home Assistant host and long-lived access token.

### Arm Blink

```bash
curl -X POST \
  http://<YOUR_HOME_ASSISTANT_HOST>:8123/api/services/alarm_control_panel/alarm_arm_away \
  -H "Authorization: Bearer <YOUR_LONG_LIVED_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "alarm_control_panel.blink_armstrong"}'
```

### Disarm Blink

```bash
curl -X POST \
  http://<YOUR_HOME_ASSISTANT_HOST>:8123/api/services/alarm_control_panel/alarm_disarm \
  -H "Authorization: Bearer <YOUR_LONG_LIVED_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "alarm_control_panel.blink_armstrong"}'
```