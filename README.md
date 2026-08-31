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
- Lutron light and scene control through Home Assistant — on/off/toggle, brightness
  percentage, and scene activation, with friendly aliases — see [Lutron](#lutron)
- **SOS** blinker for any Lutron light or switch — on/off in real seconds, run in
  the background, always ending off — see [SOS](#sos)
- Lutron **light status report** as a plain text table, read in one request —
  see [Light status](#light-status)
- Home Assistant integration switchable by feature — turn the alarm panel, the phone
  notifications, or Lutron off entirely without touching config — see [Switches](#switches)
- Multiple homes — an optional `home` field on the leaving/arriving and presence
  webhooks keeps a separate household, with its own notification text, in each
  house; omit it and everything lands in the default home `AMS` — see
  [Multiple homes](#multiple-homes)
- Per-person location log — each id gets its own `state/<id>_loc.json`, written by
  `/webhook/location/log`, readable as JSON (ready for an iPhone Shortcut to open in
  Maps) or as a formatted text history, prunable by age, and able to notify your
  phone with a switch per person — see [Location log](#location-log)
- Leaving/arriving notification titles postfixed `(A)`/`(D)` for arm/disarm,
  depending on whether anyone is still home — see [Arm/disarm postfix](#armdisarm-postfix)

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
    "HA_API_KEY": "your_home_assistant_long_lived_access_token"
}
```

> `configs/home_assistant_config.json` holds a secret token and is gitignored — never commit it.

Which *entities* to act on live separately, in the tracked
**`configs/home_assistant_entities.json`** — an entity id is a name, not a
credential, so keeping it out of the gitignored file means it survives a fresh
clone and shows up in diffs:

```json
{
    "blink":  { "panel_AMS": "alarm_control_panel.blink_NAME" },
    "notify": { "target":    "mobile_app_YOUR_PHONE" },
    "lutron": {
        "lights": { "kitchen": "light.kitchen_main" },
        "scenes": { "movie":   "scene.movie_night" }
    }
}
```

Its sections are named after the Home Assistant features in
[Switches](#switches), so the two files line up.

Keys that differ per house carry the **home name**, using the same names as
`state/presence.json` — so the default home `AMS` has `panel_AMS`, and a second
house `M` adds `panel_M` beside it. One phone is shared by every home, so
`notify.target` carries no home name.

The `notify.target` entry (e.g. `mobile_app_aisingioro`) is only needed for the
phone notification webhooks (`/webhook/notify/*`); it names the Home Assistant
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
- A third event, `location_log`, holds the text for the notification a logged
  position sends — see [Notifying your phone](#notifying-your-phone-per-person).
- `{id}` (or `{name}`) in a title/message is replaced with the person's name —
  see [Who left / arrived](#who-left--arrived) below.
- An optional `homes` block inside an event gives a named home its own title,
  message, and arm/disarm flag, and `{home}` is replaced with the home's name —
  see [Multiple homes](#multiple-homes). `configs/notify_config.example.json`
  shows the shape.
- The `blink_arm` / `blink_disarm` events hold the text for the notification
  `/webhook/blink/*` sends. They take `{home}` (naming the house whose panel
  changed) and default to `Blink Control {home} (A)` / `(D)`. There is no person
  involved in arming a panel, so `{id}` is not offered there.
- Every title also ends in an **arm/disarm postfix** — see
  [Arm/disarm postfix](#armdisarm-postfix).
- Each request may also override `title`, `message`, and the `arm`/`disarm` flag
  in its JSON body. Precedence is payload > the event's `homes` block for this
  home > `configs/notify_config.json` > the built-in defaults.

### Arm/disarm postfix

Whenever a leaving or arriving webhook fires, the notification title gets a
postfix describing the household **after** that event:

| Postfix | Meaning | When                                      |
| ------- | ------- | ----------------------------------------- |
| `(A)`   | arm     | everyone's presence is `away`             |
| `(D)`   | disarm  | at least one person is still `home`       |

```text
Leaving home (D)     # somebody is still in the house
Leaving home (A)     # that was the last person out
Welcome home (D)     # arriving always leaves somebody home
```

The postfix is appended whatever the title's source (payload, config, or
built-in default), and is computed from `state/presence.json` with the current
event's own state applied on top — so the last person leaving gets `(A)` even
though the store is only written afterwards. Only the event's own home is
counted, so somebody being home in another house never turns this one's postfix
into `(D)`. It is a label for your automations and notifications; it does not
itself arm or disarm the panel (the `arm` / `disarm` flags above do that). If
the presence store cannot be read, the title is sent without a postfix rather
than failing the notification.

### Who left / arrived

`/webhook/notify/leaving` and `/webhook/notify/arriving` read the person's
identity from an **`id`** field in the JSON body, and the house from an
optional **`home`** field:

```bash
curl -X POST http://localhost:5050/webhook/notify/leaving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex"}'
```

A post with no `id` (or a blank one) is attributed to **`娜`**, and one with no
`home` to **`AMS`** — see [Multiple homes](#multiple-homes).

Each event is persisted per home and per person in **`state/presence.json`**, so
the current home/away state survives restarts. The file is created
automatically and is gitignored (it is runtime state):

```json
{
    "homes": {
        "AMS": {
            "people": {
                "娜":   { "state": "home", "event": "arriving_home", "last_updated": "2026-08-03 18:42:11.482" },
                "Alex": { "state": "away", "event": "leaving_home",  "last_updated": "2026-08-03 08:07:53.119" }
            }
        },
        "M": {
            "people": {
                "Sam": { "state": "home", "event": "arriving_home", "last_updated": "2026-08-19 09:12:00.001" }
            }
        }
    },
    "last_modified": "2026-08-03 18:42:11.482"
}
```

Leaving sets `"away"`, arriving sets `"home"`. Other jobs can read or write it
through `jobs/presence_state.py` (`resolve_person`, `resolve_home`, `get_state`,
`all_states`, `all_homes`, `anyone_home`, `set_state`). Every accessor takes an
optional `home=` argument that defaults to `AMS`.

> A presence file written before multi-home support — a top-level `people` map
> with no `homes` — is read as home `AMS` and rewritten in the nested shape by the
> next write. Nothing has to be converted by hand.

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

# Another house, or every house at once
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?home=M"
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?home=all"

# Structured form, when you want the values rather than the text
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?format=json"
# -> {"status":"ok","home":"A","count":2,"home_id":["娜"],"away_id":["Alex"],"people":{…},"message":"Presence — 2 people\n…"}

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
  `?home=<home>` for one house (`?home=all` for every house), `?format=json` for
  the structured payload. Also accepts POST with the same fields in a JSON body.
- `read` (the webhook) — no `id` returns everyone plus `home_id`/`away_id` name
  lists; an `id` returns just that person (`presence`/`state` are `null` if never
  seen). `home` names the house the result describes. Every read includes
  `message`, the display-ready text.
- `write` — requires `state`; `id` defaults to `娜`, `home` to `AMS`, and `event`
  to `manual_write`. `state` accepts `home`/`in`/`true` and
  `away`/`left`/`out`/`not_home`/`false`.
- The two `/webhook/presence/*` paths are the single `presence_webhook` job, so
  `POST /jobs/presence_webhook/disable` turns the pair off. `GET /presence` is a
  management endpoint (like `/logs/{type}/read`) and is not affected by that
  switch.
- Nothing here arms or disarms the alarm panel — `presence/write` is bookkeeping
  only. The panel is changed by `/webhook/blink/*` and, when enabled in
  `configs/notify_config.json`, by `/webhook/notify/*`.

### Multiple homes

Every presence and leaving/arriving request takes an optional **`home`** field
naming the house it belongs to. Leave it out and the request lands in the
default home **`AMS`**, which is exactly how this server behaved before it knew
about more than one house — so nothing needs changing for a single-home setup.

```bash
# Sam arrives at home M
curl -X POST http://localhost:5050/webhook/notify/arriving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Sam", "home": "M"}'
```

Homes are independent namespaces: the same `id` in two homes is two separate
entries, and one can be `home` while the other is `away`. The `(A)`/`(D)` title
postfix counts only the event's own home.

Give a home its own notification text with a `homes` block inside the event in
`configs/notify_config.json`. Every key in the block is optional, so a home can
override just the title and inherit the shared message; a home with no block at
all uses the shared text unchanged. `{home}` is replaced with the home's name:

```json
{
    "leaving_home": {
        "title": "がいしゅつ",
        "message": "{id}さんが家を出ました。お気をつけて！",
        "arm": false,
        "homes": {
            "M": { "title": "M — がいしゅつ", "message": "{id}さんが{home}を出ました。", "arm": true }
        }
    }
}
```

Resolution precedence for the title, message, and arm/disarm flag is:
**payload > the event's `homes` block for this home > the event's shared entry >
built-in default.** `configs/notify_config.example.json` is a ready-made
template showing the whole shape.

Reading follows the same rule — no `home` means home `AMS`:

```bash
curl -H "X-Webhook-Secret: your-shared-secret-here" "http://localhost:5050/presence?home=all"
```

```text
Presence — 3 people across 2 homes
---------------------------------------------------------------
[AMS] Home (1): 娜   Away (1): Alex
[M]   Home (1): Sam  Away (0): -

[AMS] Alex  away  since 2026-08-03 20:04:55.545  (leaving_home)
[AMS] 娜    home  since 2026-08-03 20:04:55.546  (arriving_home)
[M]   Sam   home  since 2026-08-19 09:12:00.001  (arriving_home)
```

Notes and limits:

- **Each home arms its own panel.** The panel comes from `blink.panel_<home>` in
  `home_assistant_entities.json`, so a home-M event with `disarm: true` disarms
  `panel_M`. A home with no `panel_<home>` entry is an **error** — it never falls
  back to another house's panel.
- **Every home notifies the same phone.** `notify.target` carries no home name,
  so the notification text is per-home but the destination is not.
- **`all` is a reserved home name.** It is how a read asks for every house
  (matched case-insensitively), so writing to it is rejected.
- **Home names are case-sensitive and created on first write.** `{"home": "m"}`
  makes a new home rather than matching `M` — the same is already true of
  person ids.
- **Unknown homes read as empty**, not as an error, so a dashboard polling a
  house that has seen no traffic yet still gets a normal response.
- `/webhook/location/*` is unaffected — a logged position is keyed by person
  only, not by home.

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
| POST   | `/webhook/blink/arm`          | Arm a home's alarm panel + notify your phone; `{"home": "<home>"}` |
| POST   | `/webhook/blink/disarm`       | Disarm a home's alarm panel + notify your phone; `{"home": "<home>"}` |
| POST   | `/webhook/log`                | Write a log entry (see [Logging.md](docs/Logging.md)) |
| POST   | `/webhook/upload`             | Upload files, multipart/form-data (see [Uploads.md](docs/Uploads.md)) 🔒 |
| POST   | `/webhook/notify/leaving`     | Arm the panel (optional) + notify you're leaving home; `{"id": "<person>", "home": "<home>"}` 🔒 |
| POST   | `/webhook/notify/arriving`    | Disarm the panel (optional) + notify you're arriving home; `{"id": "<person>", "home": "<home>"}` 🔒 |
| POST   | `/webhook/presence/read`      | Read who's home / away, JSON body; `home` selects a house or `"all"` (see [Who left / arrived](#who-left--arrived)) 🔒 |
| POST   | `/webhook/presence/write`     | Set a person's home/away state by hand, in a given `home` 🔒 |
| GET    | `/presence`                   | Read who's home / away as text; `?id=`, `?home=`, `?home=all`, `?format=json` 🔒 |
| POST   | `/webhook/location/log`       | Log a location for a person (see [Location log](#location-log)) 🔒 |
| POST   | `/webhook/location/fetch`     | Read back a person's last location, JSON body 🔒 |
| POST   | `/webhook/location/history`   | Read a person's whole history, formatted as text 🔒 |
| POST   | `/webhook/location/purge`     | Trim history to `records` (default 10) or `days` 🔒 |
| GET    | `/location`                   | Read back a person's last location as JSON; `?id=`, `?n=` 🔒 |
| GET    | `/location/history`           | Read a person's location history as text; `?id=`, `?n=` 🔒 |
| POST   | `/webhook/lutron/light`       | Turn a Lutron light on/off/toggle, set brightness % (see [Lutron](#lutron)) 🔒 |
| POST   | `/webhook/lutron/scene`       | Activate a Lutron scene 🔒 |
| POST   | `/webhook/lutron/status`      | Report every configured light's state as text (see [Light status](#light-status)) 🔒 |
| POST   | `/webhook/lutron/sos`         | Blink a light on/off as an SOS signal, ending off (see [SOS](#sos)) 🔒 |
| GET    | `/location/notify`            | List each person's location-notification switch 🔒 |
| POST   | `/location/notify/{id}/enable`  | Notify this person's logged positions 🔒 |
| POST   | `/location/notify/{id}/disable` | Stop notifying for this person 🔒 |
| POST   | `/location/notify/{id}/toggle`  | Flip this person's notification switch 🔒 |
| GET    | `/blink/notify`               | List the arm/disarm notification switches 🔒 |
| GET    | `/ha`                         | List the Home Assistant feature switches 🔒 |
| POST   | `/ha/{feature}/enable`        | Use this HA feature (`blink`/`notify`/`lutron`) 🔒 |
| POST   | `/ha/{feature}/disable`       | Stop using this HA feature entirely 🔒 |
| POST   | `/ha/{feature}/toggle`        | Flip this HA feature switch 🔒 |
| POST   | `/blink/notify/{action}/enable`  | Notify on this blink action (`arm`/`disarm`) 🔒 |
| POST   | `/blink/notify/{action}/disable` | Stop notifying on this blink action 🔒 |
| POST   | `/blink/notify/{action}/toggle`  | Flip this blink action's notification switch 🔒 |
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

# Arming/disarming also notifies your phone. Its title/message come from
# configs/notify_config.json under "blink_arm"/"blink_disarm", and each action has
# an on/off switch in configs/notify_switches.json ("blink_control").
# An optional "home" picks which panel: blink.panel_<home> in
# configs/home_assistant_entities.json. Omit it for the default home.
curl -X POST http://localhost:5050/webhook/blink/arm \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"home": "M"}'


# Notify your phone (title/message default to configs/notify_config.json; override per request)
# "id" names who left/arrived and is recorded in state/presence.json (defaults to "娜")
# "home" names the house (defaults to "A") — see Multiple homes
curl -X POST http://localhost:5050/webhook/notify/leaving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex"}'

curl -X POST http://localhost:5050/webhook/notify/arriving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "title": "Welcome back", "message": "Kettle is on, {id}"}'

curl -X POST http://localhost:5050/webhook/notify/arriving \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Sam", "home": "M"}'

# Toggle a job or log type (secret required)
curl -X POST http://localhost:5050/jobs/log/toggle \
  -H "X-Webhook-Secret: your-shared-secret-here"

curl -X POST http://localhost:5050/logs/blink/disable \
  -H "X-Webhook-Secret: your-shared-secret-here"
```

## Location log

One job with four webhooks (`jobs/location_webhook.py`, the same shape as
`presence_webhook.py`) — the location counterpart of the `log` job: **`log`**
records where somebody is (and notifies your phone), **`fetch`** reads back the
latest position as JSON, **`history`** renders the whole history as text, and
**`purge`** trims it to the newest N records (or to a number of days). Nothing
goes into `logs/default.log` or any other text log — each person's positions live
in their own JSON file under `state/`.

Every one of them takes an **`id`** and defaults it the same way the presence
webhooks do: a missing, blank, or non-string `id` is attributed to **`娜`**.

```bash
# Log a position
curl -X POST http://localhost:5050/webhook/location/log \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "latitude": 37.334606, "longitude": -122.009102,
       "address": "Apple Park, Cupertino", "time": "2026-08-18 09:15:23.123"}'

# Read the latest one back
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location?id=Alex"
```

```json
{
    "status": "ok",
    "id": "Alex",
    "found": true,
    "latitude": 37.334606,
    "longitude": -122.009102,
    "address": "Apple Park, Cupertino",
    "time": "2026-08-18 09:15:23.123",
    "recorded_at": "2026-08-18 09:15:23.980",
    "trigger": "arrived home",
    "maps_url": "https://maps.apple.com/?ll=37.334606,-122.009102&q=Apple+Park%2C+Cupertino",
    "google_maps_url": "https://www.google.com/maps?q=37.334606,-122.009102",
    "message": "Alex was at Apple Park, Cupertino at 2026-08-18 09:15:23.123",
    "file": "Alex_loc.json"
}
```

### Logging a location (`log`)

| Field       | Required | Notes                                                        |
| ----------- | -------- | ------------------------------------------------------------ |
| `id`        | no       | Who this position belongs to; defaults to `娜`, same as presence |
| `latitude`  | **yes**  | −90…90; also accepts `lat`, and numeric strings               |
| `longitude` | **yes**  | −180…180; also accepts `lon` / `lng` / `long`                 |
| `address`   | no       | Free text; becomes the Maps pin label. Stored as `null` if omitted |
| `time`      | no       | The caller's own timestamp, stored **verbatim**; defaults to now |
| `trigger`   | no       | Why it was logged — "arrived home", "periodic", "manual". Alias `reason` |

A missing or out-of-range coordinate is reported as a JSON error and nothing is
written. Coordinates may arrive as strings, because Shortcuts sends every field
as text.

**`trigger`** is free text saying what caused the log, and once given it follows
the entry everywhere:

```bash
curl -X POST http://localhost:5050/webhook/location/log \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "latitude": 37.334606, "longitude": -122.009102,
       "address": "Apple Park", "trigger": "arrived home"}'
```

| Surface | With a trigger |
| --- | --- |
| the `log` response `message` | `Logged Alex at 37.334606,-122.009102 (Apple Park) at … ; trigger: arrived home` |
| the phone notification | `Alex is at Apple Park (…) — arrived home.` |
| `GET /location` | a `trigger` field, and `… at … (arrived home)` in `message` |
| `GET /location/history` | `Apple Park  [arrived home]` at the end of the row |
| `state/<id>_loc.json` | a `"trigger"` field on the entry |

Leave it out and every one of those reads exactly as it did before — no empty
brackets, no dangling dash. Blank strings count as "not given", and entries logged
before this field existed read back as `null`.

### Notifying your phone (per person)

Every logged position also pushes a notification to your phone, through the same
Home Assistant `notify` service the arrival/departure webhooks use. **Two
switches** gate it, and both must be on:

| Switch     | Where                                                          | Scope                                      |
| ---------- | -------------------------------------------------------------- | ------------------------------------------ |
| master     | `configs/job_switches.json` → `notify_phone`                   | every phone notification this server sends |
| per person | `configs/notify_switches.json` → `location_log.<id>`           | one person's logged positions              |

```bash
# Who is getting location notifications?
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify
```

```json
{
    "master": { "job": "notify_phone", "enabled": true },
    "ids": [ { "id": "Alex", "enabled": true }, { "id": "娜", "enabled": false } ]
}
```

```bash
# One person off, on, or flipped
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify/Alex/disable
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/location/notify/Alex/toggle

# Silence the lot — this is the notify_phone job, so it also covers
# /webhook/notify/leaving and /webhook/notify/arriving
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/jobs/notify_phone/disable
```

- A person nobody has toggled yet is **on**, and is written into
  `notify_switches.json` the first time they log a position — so they show
  up in the listing and can be turned off. The file is created automatically:

  ```json
  {
      "location_log": { "Alex": true, "娜": false },
      "blink_control": { "arm": true, "disarm": true },
      "last_modified": "2026-08-18 21:04:11.221"
  }
  ```

- The title and message live with every other notification's text, in
  **`configs/notify_config.json`** under `location_log`:

  ```json
  "location_log": {
      "title": "位置情報を記録",
      "message": "{id} is at {address} ({time}).",
      "message_with_trigger": "{id} is at {address} ({time}) — {trigger}."
  }
  ```

  `message_with_trigger` is used when the logged position carries a `trigger`, and
  `message` when it does not — that way neither version ends up with an empty
  clause. If you only set `message`, a triggered position falls back to it.

  which arrives as **位置情報を記録** / *Alex is at Apple Park, Cupertino
  (2026-08-18 09:15:23.123).* Only this event's title is Japanese —
  `leaving_home` and `arriving_home` keep their own text.

  Placeholders: `{id}`/`{name}`, `{address}`, `{latitude}`, `{longitude}`,
  `{time}`, `{trigger}` and `{maps_url}`. `{address}` falls back to the coordinates when the
  logged position had none, and `{time}` is the entry's own timestamp — the one
  the caller sent, or the moment the server stored it. A request may override `title`/`message` per call, so
  precedence matches the notify webhooks: payload > `notify_config.json` >
  built-in default.
- Switched off means "do not notify", **not** "do not log" — the position is
  still stored either way.
- The outcome comes back in the `notify` field of the `/webhook/location/log`
  response (`success`, `skipped` with the reason, or `error`). A notification that
  cannot be sent never fails the write.
- Non-ASCII ids work in the path: `/location/notify/娜/disable`.

### The store

Each id gets **`state/<id>_loc.json`** (created automatically, gitignored as
runtime state):

```json
{
    "id": "Alex",
    "entries": [
        {
            "latitude": 37.334606,
            "longitude": -122.009102,
            "address": "Apple Park, Cupertino",
            "time": "2026-08-18 09:15:23.123",
            "recorded_at": "2026-08-18 09:15:23.980",
            "trigger": "arrived home"
        }
    ],
    "last_modified": "2026-08-18 09:15:23.980"
}
```

- `time` is yours; `recorded_at` is when this server wrote the entry.
- Entries **append**, newest last, capped at the newest 500 per person
  (`MAX_ENTRIES` in `jobs/location_state.py`) so a chatty phone cannot grow the
  file without bound. "Latest" therefore means most recently *logged*, not the
  largest `time`. Trim by count or age with [`purge`](#purging-old-entries-purge).
- The filename is sanitized: path separators, control characters and leading dots
  in an `id` can never write outside `state/`. Non-ASCII names (`娜_loc.json`)
  are kept as-is.
- Other jobs can read or write the store through `jobs/location_state.py`
  (`append_location`, `latest_location`, `location_entries`, `prune_locations`).
  Reading a caller's payload and building the map links live one level up, in
  `jobs/location_webhook.py`.

### Reading it back (`fetch`)

```bash
# One person, plus their 5 most recent positions
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location?id=Alex&n=5"

# The same reader as a POST webhook, for callers that prefer a JSON body
curl -X POST http://localhost:5050/webhook/location/fetch \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex"}'
```

- `GET /location` — JSON; `?id=<person>` picks whom (defaults to `娜`), `?n=<count>`
  adds an `entries` list of recent positions, newest first. Also accepts POST with
  the same fields in a JSON body.
- Two ready-to-open map links come with every found position — **`maps_url`** for
  Apple Maps (`maps.apple.com/?ll=<lat>,<lon>&q=<address>`, pin labelled with the
  address) and **`google_maps_url`** for Google Maps
  (`google.com/maps?q=<lat>,<lon>`, works on Android and in any browser). Both are
  `null` if the stored entry has no usable coordinates.
- An id with nothing logged yet is **not** an error: you get a normal `200` with
  `"found": false` and null fields, so a Shortcut does not fail on a `404`.
- The four `/webhook/location/*` paths are the single `location_webhook` job, so
  `POST /jobs/location_webhook/disable` turns the set off — exactly as
  `presence_webhook` covers both presence paths. Like `GET /presence`, the two
  `GET /location*` endpoints are management endpoints and are not affected by that
  switch (the `/webhook/location/*` paths are).

### The whole history as text (`history`)

`fetch` only answers "where are they now". For "where have they been", read
the history — this one is **plain text**, not JSON, like `/logs/{type}/read`:

```bash
curl -H "X-Webhook-Secret: your-shared-secret-here" \
  "http://localhost:5050/location/history?id=Alex"
```

```text
Location history — Alex — 3 entries (newest first)
------------------------------------------------------------------------
2026-08-18 09:15:23.123    37.334606, -122.009102   Apple Park, Cupertino
2026-08-17 20:04:55.545    51.501400,   -0.141900   Buckingham Palace
2026-08-16 08:07:53.119    37.331800, -122.031200   -
```

- Everything is returned by default; `?n=<count>` caps it at the most recent
  `count` entries and the header then reads `2 of 340 entries`.
- Rows are **newest first**, in the order they were logged — the store is never
  re-sorted by the `time` column, so an entry sent with a wrong clock stays where
  it was logged.
- The column shown is each entry's own `time`. An entry with no address shows `-`;
  a hand-edited entry missing a coordinate shows `?` rather than breaking the
  table.
- `POST /webhook/location/history` returns the same text in the `message` field, for
  callers that want a JSON body.

### Purging old entries (`purge`)

Two ways to say what to keep — **by count** (the default) or **by age**:

```bash
# Keep Alex's 25 most recent positions, delete the rest
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "records": 25}'

# Keep the last 30 days instead
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"id": "Alex", "days": 30}'

# Neither: keeps the 10 most recent
curl -X POST http://localhost:5050/webhook/location/purge \
  -H "X-Webhook-Secret: your-shared-secret-here"
```

```json
{
    "status": "ok",
    "id": "Alex",
    "mode": "records",
    "records": 25,
    "days": null,
    "cutoff": null,
    "removed": 5,
    "kept": 25,
    "undated": 0,
    "message": "Purged 5 entries beyond the 25 most recent for Alex; 25 kept."
}
```

| Input                  | Keeps                                  | Mode      |
| ---------------------- | -------------------------------------- | --------- |
| `{"records": 25}`      | the 25 most recently logged entries    | `records` |
| `{"days": 30}`         | everything logged in the last 30 days  | `days`    |
| both                   | **records wins**, `days` is ignored    | `records` |
| neither                | the 10 most recent (`DEFAULT_RECORDS`) | `records` |

- **`records` beats `days` whenever both are passed.** The response echoes both
  inputs and `mode` tells you which rule was applied — in records mode `cutoff` is
  `null` and the message ends `(days ignored)`. Check `records`/`days` in the
  response to confirm the server read what you sent.
- Either field also works as a query parameter —
  `POST /webhook/location/purge?id=Alex&records=3` — see
  [Configuration](#configuration).
- `records` may be a numeric string and must be a whole number ≥ 0; `records: 0`
  empties the history (the file stays). `keep` works as an alias.
- `days` may be any number ≥ 0 — `nan`, `inf` and negatives are refused. `days: 0`
  means "everything up to now". It has no default: leave it out and the record
  count applies instead.
- Counting by records is **positional** — it ignores timestamps entirely, so it
  works even on entries whose `time` is unparseable.
- Ageing by days uses **`recorded_at`**, the timestamp this server wrote, falling
  back to the caller's `time` only if `recorded_at` is missing. A phone with a
  wrong clock therefore cannot talk the server into deleting fresh data.
- In days mode, an entry whose timestamp cannot be parsed at all is **kept** and
  counted in `undated` — deleting data the server cannot date would be worse than
  keeping it.
- It only ever touches the one id's file. There is no GET form: purging is
  destructive, so it is POST-with-the-secret only.
- To run it on a schedule, point cron (or a Home Assistant automation) at the
  webhook:

  ```bash
  # 04:00 daily, keep the last 30 days
  0 4 * * * curl -sS -X POST http://localhost:5050/webhook/location/purge \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex", "days": 30}'
  ```

### Opening it in Maps from an iPhone Shortcut

The response's `maps_url` is a ready-to-open Apple Maps link, so the Shortcut is
three actions:

1. **Get Contents of URL** — `https://<your-server>/location?id=Alex`, method
   `GET`, with header `X-Webhook-Secret: your-shared-secret-here`.
2. **Get Dictionary Value** — key `maps_url` (or `google_maps_url` to open Google
   Maps instead).
3. **Open URLs** — Maps opens on the pin, labelled with the address.

To log a position from the phone instead, use **Get Current Location** and POST
its `Latitude` / `Longitude` to `/webhook/location/log`.

## Lutron

Control Lutron lights and scenes through Home Assistant. Home Assistant has no
Lutron-specific API — both the `lutron` (RadioRA 2 / HomeWorks QS) and
`lutron_caseta` (Caséta / RA3) integrations register ordinary entities, so this
job calls the standard services:

| Lutron device | HA entity | Service called |
| ------------- | --------- | -------------- |
| Dimmer | `light.*` | `light.turn_on` / `turn_off` / `toggle` |
| Non-dim switch | `switch.*` | `switch.turn_on` / `turn_off` / `toggle` |
| Scene / keypad button | `scene.*` | `scene.turn_on` |

The service domain is taken from the **entity id itself**, not assumed — Lutron's
non-dimming switches arrive as `switch.*` rather than `light.*`, and they cannot
dim, so a brightness sent to one is an error rather than a silently dropped field.

### Naming things

Give an entity a short alias in the `lutron` section of
**`configs/home_assistant_entities.json`**, or pass its full Home Assistant
entity id. Anything containing a `.` is treated as an entity id, so both work and
there is no mode flag:

```json
{
    "lutron": {
        "lights": {
            "kitchen": "light.kitchen_main",
            "living":  "light.living_room_dimmer",
            "hallway": "switch.hallway_lights"
        },
        "scenes": {
            "movie":     "scene.movie_night",
            "goodnight": "scene.goodnight"
        }
    }
}
```

Aliases keep entity ids in one place, so renaming something in Home Assistant is
one config edit rather than a hunt through every Shortcut.
`configs/home_assistant_entities.example.json` is a filled-in template. A missing
or malformed file is not fatal — you lose the aliases, and raw entity ids still
work.

To list your own entity ids and the exact fields your Home Assistant accepts:

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" http://<hostID>:8123/api/states \
  | python3 -c 'import json,sys; [print(e["entity_id"], "|", e["attributes"].get("friendly_name","")) for e in json.load(sys.stdin) if e["entity_id"].split(".")[0] in ("light","switch","scene","cover")]'
```

### Lights

```bash
curl -X POST http://localhost:5050/webhook/lutron/light \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"light": "kitchen", "brightness": 40}'
```

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `light` | yes | An alias from `home_assistant_entities.json`, or a full entity id |
| `state` | no | `on` / `off` / `toggle` (also JSON `true`/`false`); defaults to `on` |
| `brightness` | no | **Percentage 0-100**, sent as HA's `brightness_pct`. Lights only |
| `transition` | no | Fade time in seconds |

```bash
# All the shapes
-d '{"light": "kitchen", "brightness": 40}'                    # on, at 40%
-d '{"light": "kitchen", "state": "off", "transition": 2}'      # off over 2s
-d '{"light": "kitchen", "state": "toggle"}'                    # flip it
-d '{"light": "hallway", "state": "on"}'                        # a switch.* entity
-d '{"light": "light.unlisted_lamp", "brightness": 100}'        # raw entity id
```

### Scenes

```bash
curl -X POST http://localhost:5050/webhook/lutron/scene \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"scene": "movie"}'
```

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `scene` | yes | An alias from `home_assistant_entities.json`, or a full entity id |
| `transition` | no | Fade time in seconds |

**Home Assistant scenes cannot be turned off or toggled** — activating is the only
thing a scene does. To undo one, activate another (a `goodnight` scene beside a
`movie` scene). A `state` field on a scene request is ignored rather than an error.

### Light status

Report what Home Assistant currently says about every light in your `lutron`
aliases — state, brightness and friendly name — as a text table, the way
`GET /presence` reports people.

```bash
curl -X POST http://localhost:5050/webhook/lutron/status \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{}'
```

```text
Lutron lights — 4 lights
---------------------------------------------------------------------
On (2): kitchen, living
Off (1): hallway
Missing/unavailable (1): porch

hallway   off             -  switch.hallway_lights     Hallway Lights
kitchen   on           100%  light.kitchen_main        Kitchen Main
living    on            40%  light.living_room_dimmer  Living Room
porch     missing         -  light.porch_old
```

It takes no fields. Alongside `message` (the table above) the result carries the
structured form, like the presence read:

```json
{"status": "ok", "count": 4,
 "on": ["kitchen", "living"], "off": ["hallway"], "other": ["porch"],
 "lights": {
   "living": {"entity_id": "light.living_room_dimmer", "state": "on",
              "brightness": "40%", "name": "Living Room"}
 },
 "message": "Lutron lights — 4 lights\n…"}
```

Notes:

- **One request, however many lights.** Every alias is resolved from a single
  `GET /api/states`, so the cost does not grow with your alias list.
- **A stale alias shows up as `missing`** rather than being dropped — if you
  renamed or recreated something in the Lutron app, this is where you will see it.
  `unavailable` means Home Assistant knows the entity but cannot reach the device.
- **Brightness is a percentage**, converted from the 0-255 Home Assistant reports.
  Switches have no brightness and show `-`.
- The `Missing/unavailable` line only appears when something is wrong, so a healthy
  report is two summary lines.
- Read-only: it never calls a service, so it cannot change a light.

### SOS

Blink a light or switch on and off as an attention signal. It always ends **off**.

```bash
curl -X POST http://localhost:5050/webhook/lutron/sos \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" \
  -d '{"light": "kitchen"}'
```

```json
{"status": "started", "entity_id": "light.kitchen_main", "duration": 10.0,
 "interval": 2.0, "calls": 6, "estimated_seconds": 10.0,
 "message": "Blinking light.kitchen_main for 10s every 2s, ending off"}
```

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `light` | yes | An alias from `home_assistant_entities.json`, or a full entity id |
| `duration` | no | Total seconds to blink for; default 10, range 2-300 |
| `interval` | no | Seconds per on and per off; default 2, range 1-60 |

The default is 10 seconds alternating every 2 seconds — three on, three off:

```text
ON  off ON  off ON  off
 2   2   2   2   2   0     seconds  (6 service calls, ends off)
```

**Why seconds and not milliseconds.** Lutron hardware needs real time to switch.
A sub-second blink is either invisible or arrives as a dim flicker, so the
interval is in whole seconds and there is no Morse-code timing to get wrong.
Every step still sends `transition: 0` so a dimmer's fade does not soften the
edge, and `switch.*` entities — which snap, and take no `transition` — make the
crispest target.

**It returns immediately.** The blinking runs on a background thread, because
holding the request open for 10-300 s would time out an iPhone Shortcut. The
response says what was *started*; the outcome lands in the `blink` log:

```bash
curl -H "X-Webhook-Secret: your-shared-secret-here" http://localhost:5050/logs/blink/read
# -> LUTRON SOS light.kitchen_main: 10s at 2s, 6 calls, 0 failed, ended off
```

Notes and limits:

- **Ends off, always.** If the last period was an on, a closing off is appended.
  The previous state is not read or restored — the light is dark afterwards even
  if it was on before.
- **One SOS per entity at a time.** A second request while one is blinking returns
  `Already running`. A different entity is unaffected, and the guard is released
  even if the thread crashes.
- **An `interval` longer than the `duration` is rejected**, since it would give a
  single on with nothing after it.
- Gated by the `lutron` feature switch and the `home_assistant_lutron` job switch,
  exactly like the light and scene endpoints.

### Switches and errors

Two switches gate this job, like the other Home Assistant integrations:

- `POST /ha/lutron/disable` — the whole integration stops; both endpoints return
  `"skipped"` and no HTTP request is made.
- `POST /jobs/home_assistant_lutron/disable` — both webhook paths stop responding, returning
  `403 {"status": "disabled"}`.

Every problem is reported in the JSON result rather than raised, and nothing
reaches Home Assistant when a request is rejected:

| Error | Cause |
| ----- | ----- |
| `Missing light` / `Missing scene` | no `light`/`scene`/`entity` field |
| `Unknown light` / `Unknown scene` | not an alias and not an entity id; the message lists the known aliases |
| `Invalid state` | `state` was not `on`/`off`/`toggle` |
| `Invalid brightness` | not a number, or outside 0-100 |
| `Invalid transition` | not a number, or negative |
| `Not dimmable` | `brightness` sent to a `switch.*` entity |
| `Conflicting request` | `brightness` combined with `state: "off"` |
| `Wrong entity domain` | a scene sent to `/light`, or a light sent to `/scene` |
| `Invalid duration` / `Invalid interval` | an SOS field outside its range, or interval > duration |
| `Already running` | an SOS is already blinking that entity |

## Switches

Features are partitioned by switch file, one file per level of the system:

```
home_assistant_switches.json   do we talk to Home Assistant at all?
  ├── blink                    ... to arm/disarm the Blink alarm panel
  ├── notify                   ... to push notifications to a phone
  └── lutron                   ... to control Lutron lights and scenes

job_switches.json              which webhook jobs are live
log_switches.json              which log types are written
notify_switches.json           which notifications are actually sent
```

Two kinds of JSON file live in `configs/`, and the suffix tells you which:

- **`*_switches.json`** — runtime on/off state. Entries appear automatically the
  first time something is used, and you flip them over HTTP. Safe to edit, but
  the endpoints are easier.
- **`*_config.json`** — hand-written configuration. Nothing writes to these.

Most features are gated by **two** switches, and both must be on for anything to
happen: a **master** switch for the whole job, and a **per-key** switch for one
log type, person, or action.

| What it controls | File → section → key | Toggle with |
| ---------------- | -------------------- | ----------- |
| Arming/disarming the panel through Home Assistant, from **any** path | `home_assistant_switches.json` → `features.blink` | `POST /ha/blink/enable\|disable\|toggle` |
| Sending **any** phone notification through Home Assistant | `home_assistant_switches.json` → `features.notify` | `POST /ha/notify/enable\|disable\|toggle` |
| Controlling Lutron lights and scenes through Home Assistant | `home_assistant_switches.json` → `features.lutron` | `POST /ha/lutron/enable\|disable\|toggle` |
| A whole job, including every webhook it owns | `job_switches.json` → `jobs.<job>` | `POST /jobs/<job>/enable\|disable\|toggle` |
| One log type (`blink`, `upload`, `default`) | `log_switches.json` → `types.<type>` | `POST /logs/<type>/enable\|disable\|toggle` |
| One person's location notification | `notify_switches.json` → `location_log.<id>` | `POST /location/notify/<id>/enable\|disable\|toggle` |
| The arm / disarm notification | `notify_switches.json` → `blink_control.<action>` | `POST /blink/notify/<action>/enable\|disable\|toggle` |

The master switch for **every** phone notification is the `notify_phone` job, so
`POST /jobs/notify_phone/disable` silences the leaving/arriving, location, and
arm/disarm notifications at once.

**`home_assistant_switches.json` is the top tier.** Each feature is checked at the
one place that Home Assistant API is called, so a single switch covers every
caller:

| Feature | Checked in | Covers |
| ------- | ---------- | ------ |
| `blink` | `set_alarm()` | `/webhook/blink/*` **and** the leaving/arriving webhooks |
| `notify` | `notify_phone()` | the leaving/arriving, location, and arm/disarm notifications |
| `lutron` | the `jobs.home_assistant_lutron` handlers | `/webhook/lutron/*` |

A feature that is off makes the call a no-op reporting `"skipped"` — no HTTP
request, and `home_assistant_config.json` is not even read, so a feature can be
switched off before it is configured. The two are independent: you can keep the
notifications while the panel is switched off, which is useful while a Home
Assistant problem is being waited out.

```bash
# Stop touching the alarm panel, keep every notification
curl -X POST -H "X-Webhook-Secret: your-shared-secret-here" \
  http://localhost:5050/ha/blink/disable

# What is on?
curl -H "X-Webhook-Secret: your-shared-secret-here" http://localhost:5050/ha
# -> {"features":[{"feature":"blink","enabled":false},{"feature":"lutron","enabled":true},{"feature":"notify","enabled":true}]}
```

An unknown key counts as **on** and is written to the file the first time it is
checked, so it shows up in the listing and can be turned off afterwards. That
also means a missing switch file is not an error — everything is simply enabled.

### What is *not* a switch

| File | Role |
| ---- | ---- |
| `config.json` | Routing table: webhook path → job module + function, and `require_secret` per path |
| `notify_config.json` | The **text** of every notification (titles, messages, per-home overrides) |
| `home_assistant_config.json` | Home Assistant URL, token, panel entity, notify target (gitignored) |
| `webhook_secret.json` | The shared webhook secret (gitignored) |

`notify_config.json` and `notify_switches.json` are the pair to keep straight:
one says **what** a notification says, the other says **whether** it is sent.

Two flags inside `notify_config.json` do behave like switches, but they gate the
**alarm panel**, not a notification: `arm` on `leaving_home` and `disarm` on
`arriving_home` decide whether those webhooks touch the panel at all. Turning off
a `blink_control` switch only silences the notification — `/webhook/blink/*` still
arms and disarms.

## Configuration

**`configs/config.json`** maps webhook paths to job modules:

```json
{
    "webhooks": [
        {
            "path": "/webhook/blink/arm",
            "module": "jobs.home_assistant_blink",
            "require_secret": true
        }
    ]
}
```

- `module` — the job module that handles the request (must expose a `run(payload)` function)
- `function` — the function to call, when a module handles several webhooks (defaults to `run`)
- `require_secret` — when `true`, the request must include the shared secret in the `X-Webhook-Secret` header; `false` disables auth for that webhook

A webhook's inputs may be sent **either as a JSON body or as query parameters**,
and the body wins when a field appears in both:

```bash
curl -X POST "http://localhost:5050/webhook/location/purge?id=Alex&records=3" \
  -H "X-Webhook-Secret: your-shared-secret-here"

curl -X POST http://localhost:5050/webhook/location/purge \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-shared-secret-here" -d '{"id": "Alex", "records": 3}'
```

A JSON body is parsed even if you forget `Content-Type: application/json` —
otherwise it would be dropped silently and the job would run with its defaults.
The one exception is `/webhook/upload`, whose multipart body belongs to the job
itself (see [Uploads.md](docs/Uploads.md)); send its fields as query parameters or
form fields.

**`configs/webhook_secret.json`** holds the single shared secret used by every
authenticated webhook. It is gitignored — copy it from the example and fill it in:

```json
{
    "WEBHOOK_SECRET": "a-long-random-string"
}
```

**`configs/notify_switches.json`** holds the on/off switches for every phone
notification this server sends — one per person for logged positions
(see [Notifying your phone](#notifying-your-phone-per-person)) and one per action
for the arm/disarm notification (see [Switches](#switches)). It is created
automatically and updated through the `/location/notify` and `/blink/notify`
endpoints:

```json
{
    "location_log": { "Alex": true },
    "blink_control": { "arm": true, "disarm": true }
}
```

**`configs/home_assistant_entities.json`** holds every Home Assistant entity this
server acts on — the alarm panel, the phone notify target, and the Lutron alias
maps. It is tracked (no secrets), and its sections are named after the Home
Assistant features:

```json
{
    "blink":  { "panel_AMS": "alarm_control_panel.blink_armstrong" },
    "notify": { "target":    "mobile_app_aisingioro" },
    "lutron": { "lights": {}, "scenes": {} }
}
```

**`configs/home_assistant_switches.json`** decides whether this server uses Home
Assistant at all — `blink` for the alarm panel, `notify` for phone notifications,
and `lutron` for lights and scenes (see [Switches](#switches)). It is created
automatically and updated through the `/ha` endpoints:

```json
{
    "features": { "blink": true, "notify": true, "home_assistant_lutron": true }
}
```

**`configs/job_switches.json`** tracks which jobs are enabled. It is created automatically
and updated through the `/jobs` endpoints — you rarely edit it by hand:

```json
{
    "jobs": {
        "home_assistant_blink": true
    }
}
```

## Testing

```bash
python3 jobs/home_assistant_blink.py        # exercise the job directly
python3 tests/test_job_management.py        # job enable/disable logic
python3 tests/test_log_engine.py            # logging engine tests
python3 tests/test_file_upload.py           # file upload job tests
python3 tests/test_lutron.py                # Lutron light/scene job + shared HA API caller
python3 tests/test_notify_phone.py          # phone notification job tests (incl. per-home text)
python3 tests/test_presence_webhook.py      # presence read/write webhook tests (incl. multi-home)
python3 tests/test_location.py              # location log/fetch/history/purge + notify tests
python3 app.py --debug                      # then hit endpoints with curl
```

## Security

Webhooks with `"require_secret": true`, every state-changing management
endpoint (`/jobs/{name}/enable|disable|toggle` and
`/logs/{type}/enable|disable|toggle`), reading log contents
(`/logs/{type}/read`), reading presence (`GET /presence`), reading a logged
location (`GET /location`, `GET /location/history`) and the location-notification
switches (`GET /location/notify`, `/location/notify/{id}/enable|disable|toggle`)
require the shared
secret (from `configs/webhook_secret.json`) in
the `X-Webhook-Secret` header; requests without it get `401`. The remaining
read-only endpoints (`GET /jobs`, `GET /logs`, `/health`) are open. Use a
strong, random secret in production, and prefer a private network
(e.g. Tailscale) over public exposure.

## License

MIT
