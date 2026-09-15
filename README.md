# Spotify Jam Bridge

Headless container that logs into Spotify (OAuth, no browser needed at runtime), keeps a session open,
and exposes the current Jam invite link and QR code over HTTP. Works with the Windows machine off.
Designed for Home Assistant dashboards and n8n orchestration.

**Warning.** Jam has no public API. This uses the internal `social-connect` endpoints the official
clients use. They are undocumented, may change without notice, and using them from an unofficial
client is a grey area under Spotify's terms. Premium is required to host a Jam.

## What it does

| Endpoint | Method | Purpose |
|---|---|---|
| `/setup` | GET/POST | One-time OAuth login UI |
| `/jam` | GET | Current Jam as JSON (`?fresh=true` forces a Spotify call) |
| `/jam/start` | POST | Create a Jam if none is active |
| `/jam/end` | POST | End the current Jam |
| `/jam/refresh` | POST | Re-read from Spotify and re-push to HA/webhook |
| `/jam/qr.png`, `/jam/qr.svg` | GET | QR for the invite link (placeholder when idle) |
| `/health` | GET | Liveness |

A background poller checks Spotify every `POLL_INTERVAL` seconds. When the session changes it
regenerates the QR and pushes the new link to Home Assistant (`input_text`) and/or an n8n webhook.
With `AUTO_START=true` it keeps a Jam open permanently.

## Deploy (Dockhands / compose)

```
git clone <this folder> spotify-jam-bridge
cd spotify-jam-bridge
docker compose up -d --build
```

Volume `./data` holds `credentials.json` (reusable Spotify credentials, treat as a secret) and a
stable `device_id`.

Environment variables are documented in `docker-compose.yml`. Minimum: none. To push into HA set
`HA_URL`, `HA_TOKEN` (long-lived token) and `HA_ENTITY`. To push into n8n set `WEBHOOK_URL`.
Set `API_KEY` if the port is reachable from anywhere you don't trust.

## First login

1. Open `http://<host>:8080/setup`.
2. Click the Spotify login link and approve.
3. Spotify redirects to `http://127.0.0.1:5588/login?code=...`, which fails in your browser
   (that address is Spotify's fixed redirect for this client id). Copy the full URL from the address bar.
4. Paste it into the form and submit. The container stores reusable credentials and reconnects on restart.

## Home Assistant

`ha/configuration.yaml` has two integration styles:

- **Pull**: a `rest` sensor polls `/jam` and a `generic` camera shows `/jam/qr.png`. No HA token
  in the container. Simplest and recommended.
- **Push**: the container writes the link into `input_text.spotify_jam_link` via the HA API.

`ha/dashboard-card.yaml` is a Lovelace card that shows the QR only while a Jam is live, plus
start/end buttons (via `rest_command`).

## n8n

Import `n8n/spotify-jam-workflow.json`. It contains three triggers:

- Schedule: start a Jam on Friday and Saturday at 18:00 (edit the cron).
- Schedule: end it at 02:00.
- Webhook `/webhook/spotify-jam`: receives the bridge's change events (set `WEBHOOK_URL` in the
  container to this URL) and updates HA plus sends a notification.

Set these n8n environment variables: `JAM_BRIDGE_URL`, `JAM_BRIDGE_KEY` (may be empty), `HA_URL`, `HA_TOKEN`.

## Verifying the unofficial part

Two things could not be verified without a live account and must be checked on first run:

1. **Token acceptance.** The bridge sends librespot's login5 token to `social-connect`. The reference
   implementation this is based on used a web-player token. If `/jam/start` returns HTTP 401/403,
   the token flavour is rejected; open an issue in your notes and we switch to the web-player token
   path (sp_dc cookie + TOTP), which is more fragile.
2. **Playback device.** The bridge registers as a device named `DEVICE_NAME` but does not play
   audio. If `/jam/start` returns a session but the Jam ends immediately or the invite refuses to
   join, Spotify wants an active playback device. Run an official `librespot` container on the same
   account as a Spotify Connect target and start playback on it (HA `media_player` or Spotify Connect
   from your phone) before creating the Jam.

Useful checks:

```
curl -s localhost:8080/health
curl -s -X POST localhost:8080/jam/start | jq
curl -s localhost:8080/jam | jq .qr_payload
docker logs -f spotify-jam-bridge
```

`JAM_START_QUERY` lets you change the query string sent to `current_or_new` without rebuilding
(default `activate=true&alt=json`; the desktop client also sends `local_device_id=<id>&type=REMOTE`).
`USE_SHORT_LINK=false` encodes `https://open.spotify.com/socialsession/<token>` instead of a
`spotify.link` short URL.
