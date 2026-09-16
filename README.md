# Spotify Jam Bridge

A small, headless service that hosts a Spotify **Jam** from a Docker container and exposes the
invite link and QR code over HTTP, so a Home Assistant dashboard can always show a scannable,
up-to-date Jam code without any desktop or phone app running.

## Purpose

Spotify Jam lets guests add tracks to a shared queue by scanning a QR code. The official apps only
show that QR inside the desktop or mobile client, and there is no public API for it. The invite link
changes every time a Jam is created, so the code cannot simply be printed once.

This bridge solves that:

- It keeps a permanent, OAuth-authenticated Spotify session inside a container (no browser, no
  desktop app, no Windows machine).
- It creates, reads and ends Jam sessions using the same internal endpoints the official clients use.
- It renders the invite link as a QR image and publishes it over HTTP.
- It notifies Home Assistant and/or n8n whenever the Jam changes.
- It exposes a heartbeat endpoint so an uptime monitor (Gatus) can alert when the login breaks.

**Caveat.** The `social-connect` endpoints are undocumented. Spotify can change or block them at any
time, and using them from an unofficial client is a grey area under Spotify's terms. A Premium
account is required to host a Jam. Use this for a home setup, not for anything you depend on.

## How it works

```
+-------------------+        spclient (internal API)        +-------------+
| spotify-jam-bridge| <-----------------------------------> |   Spotify   |
|  librespot session|                                       +-------------+
|  FastAPI on :8080 |
+---------+---------+
          |  GET /jam, /jam/qr.png, /heartbeat        push on change (optional)
          v                                          v
  Home Assistant (rest sensor, camera)      HA input_text / n8n webhook
  Gatus (heartbeat)
  n8n (schedules start/end)
```

A background poller calls Spotify every `POLL_INTERVAL` seconds. When the Jam changes (created,
ended, new token) it regenerates the QR and pushes the new state to Home Assistant and/or a webhook.
With `AUTO_START=true` it recreates a Jam whenever none is active.

## HTTP API

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/setup` | GET, POST | key | One-time OAuth login page |
| `/logout` | POST | key | Forget stored credentials |
| `/jam` | GET | key | Current Jam as JSON. `?fresh=true` forces a live Spotify call |
| `/jam/start` | POST | key | Create a Jam (returns the existing one if already active) |
| `/jam/end` | POST | key | End the current Jam |
| `/jam/refresh` | POST | key | Re-read from Spotify and re-push to HA/webhook |
| `/jam/qr.png` | GET | key | QR of the invite link, 600x600 PNG, placeholder when idle |
| `/jam/qr.svg` | GET | key | Same as SVG |
| `/health` | GET | none | Process liveness (always 200 when the app runs) |
| `/heartbeat` | GET | none | Functional health, 200 or 503 (see Monitoring) |

"key" means: if `API_KEY` is set, send it as header `X-API-Key: <key>` or query `?key=<key>`.
Without `API_KEY` everything is open. Keep the port on your LAN either way.

Example `/jam` response:

```json
{
  "active": true,
  "session_id": "…",
  "join_token": "…",
  "join_uri": "spotify:socialsession:…",
  "join_url": "https://open.spotify.com/socialsession/…",
  "short_url": "https://spotify.link/…",
  "qr_payload": "https://spotify.link/…",
  "owner": "…",
  "member_count": 2,
  "updated_at": "2026-09-16T18:02:11+00:00",
  "last_error": null,
  "connected": true,
  "username": "yourname"
}
```

`qr_payload` is what the QR encodes: the `spotify.link` short URL when `USE_SHORT_LINK=true`
(same as the official app), otherwise the `open.spotify.com/socialsession/…` URL.

## Configuration

All settings are environment variables. Defaults are safe; nothing is required to start.

| Variable | Default | Description |
|---|---|---|
| `DEVICE_NAME` | `Jam Bridge` | Device name registered with Spotify |
| `POLL_INTERVAL` | `60` | Seconds between Spotify checks |
| `AUTO_START` | `false` | Keep a Jam open at all times |
| `USE_SHORT_LINK` | `true` | Encode a `spotify.link` short URL instead of the long URL |
| `API_KEY` | empty | Protect the API. Empty = no auth |
| `HA_URL` | empty | Home Assistant base URL, e.g. `http://192.168.0.10:8123` |
| `HA_TOKEN` | empty | HA long-lived access token |
| `HA_ENTITY` | `input_text.spotify_jam_link` | Entity written on change. `input_text.*` gets the link; any other domain gets a full state via the REST API |
| `WEBHOOK_URL` | empty | POST target (n8n) called on every change with the `/jam` JSON |
| `JAM_START_QUERY` | `activate=true&alt=json` | Query string sent to `sessions/current_or_new` |
| `SPOTIFY_USER_AGENT` | iOS client string | Sent with spclient calls |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every spclient call |
| `DATA_DIR` | `/data` | Where credentials and the device id are stored |

Persistent data in `/data`:

- `credentials.json`: reusable Spotify credentials. Treat as a password; anyone with it can use your account.
- `device_id`: stable 40-char device id so Spotify sees one device across restarts.

## Setup

### 1. Deploy

The repo contains a `Dockerfile` and `docker-compose.yml` with `${VAR}` placeholders for every
setting and a named volume `jam-data` for `/data`.

**Dockhand (Git stack)**

1. Stacks > Deploy from Git > select the repository.
2. Compose file path: `docker-compose.yml`.
3. Deploy options: **Build images on deploy = ON** (the image is built from the Dockerfile).
4. Environment variables pane: one per line, at least

   ```
   HA_URL=http://<ha-ip>:8123
   HA_TOKEN=<long-lived token>
   API_KEY=<random string>
   ```

5. Create. First build takes a minute or two.

Optional: enable the webhook so a `git push` redeploys the stack.

**Plain compose**

```
git clone https://github.com/<you>/spotify-jam-bridge
cd spotify-jam-bridge
cp .env.example .env   # edit values
docker compose up -d --build
```

### 2. Log in to Spotify (once)

1. Open `http://<host>:8080/setup` (add `?key=<API_KEY>` if set).
2. Click the Spotify login link and approve.
3. Spotify redirects to `http://127.0.0.1:5588/login?code=…`. That page fails to load in your
   browser; this is expected (it is Spotify's fixed redirect for this client id). Copy the full URL
   from the address bar.
4. Paste it into the form and submit. The page reloads showing "Logged in as <username>".

Credentials are stored in the volume; the container reconnects on every restart without user action.
`/logout` removes them.

### 3. Verify

```
curl http://<host>:8080/heartbeat            # expect 200, "ok": true, your username
curl -X POST http://<host>:8080/jam/start     # expect "active": true and a qr_payload
```

Open `http://<host>:8080/jam/qr.png` and scan it with the Spotify app on a phone. If it joins the
Jam, everything works. `docker logs -f spotify-jam-bridge` shows each poll and push.

PowerShell equivalent of the POST: `Invoke-RestMethod -Method Post http://<host>:8080/jam/start`.

### 4. Home Assistant

See `ha/configuration.yaml`. Two integration styles, use either or both:

- **Pull (recommended)**: a `rest` sensor polls `/jam` every 60 s (state `on`/`off`, attributes hold
  the link and member count) and a `generic` camera shows `/jam/qr.png`. Nothing to configure in
  the container.
- **Push**: set `HA_URL`, `HA_TOKEN`, `HA_ENTITY`; the bridge writes the link into an `input_text`
  on every change (max 255 chars, the short link fits).

`ha/dashboard-card.yaml` is a Lovelace vertical stack: QR shown only while the Jam is live, a
status line with a clickable join link, and Start/End buttons backed by `rest_command`.

Replace `10.0.0.50:8080` in the snippets with your host. Append `?key=<API_KEY>` to every URL if
you set one.

### 5. n8n

Import `n8n/spotify-jam-workflow.json`. It contains:

- Schedule trigger: `POST /jam/start` on Friday and Saturday at 18:00.
- Schedule trigger: `POST /jam/end` at 02:00.
- Webhook `/webhook/spotify-jam`: receives the bridge's change events (set `WEBHOOK_URL` in the
  container to this URL), writes the link to HA and sends a notification.

Set these n8n environment variables: `JAM_BRIDGE_URL`, `JAM_BRIDGE_KEY` (may be empty), `HA_URL`,
`HA_TOKEN`. Adjust the cron expressions to taste, or delete the schedules and let `AUTO_START` do it.

## Monitoring

### `/heartbeat`

Returns **200** with `"ok": true` when all of the following hold, otherwise **503** with a
`problems` array explaining what is wrong:

- stored credentials exist,
- the Spotify session is connected and has a username,
- the last poll succeeded (`last_error` is null),
- the last successful poll is younger than `3 × POLL_INTERVAL`.

`/heartbeat?deep=true` additionally performs a live `sessions/current` call, which catches a
revoked token immediately rather than at the next poll. Use it with an interval of a few minutes.

Example:

```json
{ "ok": false, "username": null, "connected": false, "updated_at": null,
  "problems": ["no stored credentials, OAuth required at /setup", "no successful poll yet"] }
```

### Gatus

```yaml
endpoints:
  - name: spotify-jam-bridge
    group: homelab
    url: "http://192.168.0.16:8080/heartbeat?deep=true"
    interval: 5m
    conditions:
      - "[STATUS] == 200"
      - "[BODY].ok == true"
      - "[BODY].username == yourname"
      - "[RESPONSE_TIME] < 5000"
    alerts:
      - type: <your alert type>
        failure-threshold: 2
        success-threshold: 1
        send-on-resolved: true
```

The `username` condition is the login check: if credentials are ever lost, the body carries no name
and the alert fires. `failure-threshold: 2` avoids alerts on a single transient Spotify error.

### Docker

The image has a `HEALTHCHECK` on `/health` (process liveness only). Dockhand shows it as the
container health state. For functional monitoring rely on `/heartbeat`.

### Logs

`docker logs spotify-jam-bridge`. Useful lines:

- `Connected as <user>`: session established at startup.
- `Jam changed: active=… session=…`: a change was detected and pushed.
- `Refresh failed: …` followed by `Connecting to Spotify with stored credentials`: a poll failed and
  the bridge reconnected; if it repeats every poll, check `/heartbeat` and the Spotify account.

Set `LOG_LEVEL=DEBUG` to see every spclient request and status code.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/heartbeat` 503, "no stored credentials" | Volume lost or never logged in | Redo `/setup` |
| `/jam/start` returns 502 with 401/403 | Token flavour rejected by Spotify | Try `JAM_START_QUERY=activate=true&local_device_id=<id>&type=REMOTE&alt=json` (id in `/data/device_id`) |
| Jam created but guests cannot join | Spotify wants an active playback device | Start playback on any Connect device logged in with the same account before creating the Jam |
| QR shows "No active Jam" | No session | `POST /jam/start` or set `AUTO_START=true` |
| HA camera stays blank | URL or key wrong | Open the PNG URL in a browser from the HA host's network |
| `short_url` is null | url-dispenser call failed | Harmless; `qr_payload` falls back to the long URL |

## Repository layout

```
app/
  main.py      FastAPI app, poller, endpoints
  spotify.py   librespot session, OAuth, social-connect calls
  qr.py        QR / placeholder rendering
  notify.py    Home Assistant and webhook push
ha/            HA configuration and dashboard card snippets
n8n/           importable n8n workflow
Dockerfile, docker-compose.yml, requirements.txt, .env.example
```

## Credits

Endpoint behaviour is based on the archived `eta-c/spotify-jam` desktop extension. Spotify session
handling uses `librespot-python`.
