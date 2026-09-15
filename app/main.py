import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from notify import Notifier
from qr import render_png, render_svg
from spotify import JamSession, SpotifyJam

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jam")


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = os.getenv("DATA_DIR", "/data")
API_KEY = os.getenv("API_KEY") or None
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
AUTO_START = env_bool("AUTO_START", False)
USE_SHORT_LINK = env_bool("USE_SHORT_LINK", True)

spotify = SpotifyJam(
    data_dir=DATA_DIR,
    device_name=os.getenv("DEVICE_NAME", "Jam Bridge"),
    user_agent=os.getenv("SPOTIFY_USER_AGENT", "Spotify/8.8.82 iOS/17.2 (iPhone13,4)"),
    start_query=os.getenv("JAM_START_QUERY", "activate=true&alt=json"),
    use_short_link=USE_SHORT_LINK,
)
notifier = Notifier(os.getenv("HA_URL"), os.getenv("HA_TOKEN"),
                    os.getenv("HA_ENTITY", "input_text.spotify_jam_link"),
                    os.getenv("WEBHOOK_URL"))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.jam = JamSession()
        self.updated_at = None
        self.last_error = None
        self.png = render_png(None)

    def update(self, jam: JamSession, force_push: bool = False):
        with self.lock:
            changed = jam.key() != self.jam.key()
            self.jam = jam
            self.updated_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            if changed:
                self.png = render_png(jam.qr_payload)
                log.info("Jam changed: active=%s session=%s", jam.active, jam.session_id)
        if changed or force_push:
            notifier.push(self.snapshot())

    def snapshot(self) -> dict:
        with self.lock:
            d = self.jam.to_dict()
            d.update({"updated_at": self.updated_at, "last_error": self.last_error,
                      "connected": spotify.connected, "username": spotify.username})
            return d


state = State()


def refresh(force_push: bool = False) -> dict:
    try:
        jam = spotify.current()
        if AUTO_START and not jam.active:
            log.info("No active Jam; AUTO_START is on, creating one")
            jam = spotify.start()
        state.update(jam, force_push)
    except Exception as e:
        log.error("Refresh failed: %s", e)
        with state.lock:
            state.last_error = str(e)
        # Try to re-establish the session once; the next poll will use it.
        try:
            if spotify.has_credentials:
                spotify.connect()
        except Exception as e2:
            log.error("Reconnect failed: %s", e2)
    return state.snapshot()


def poller():
    while True:
        if spotify.has_credentials:
            refresh()
        time.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if spotify.has_credentials:
        try:
            spotify.connect()
        except Exception as e:
            log.error("Initial connect failed: %s", e)
    threading.Thread(target=poller, daemon=True, name="poller").start()
    yield


app = FastAPI(title="Spotify Jam Bridge", lifespan=lifespan)


def auth(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("x-api-key") or request.query_params.get("key")
    if key != API_KEY:
        raise HTTPException(401, "invalid api key")


# ---------- health ----------
@app.get("/health")
def health():
    return {"ok": True, "credentials": spotify.has_credentials, "connected": spotify.connected}


# ---------- setup (OAuth) ----------
@app.get("/setup", response_class=HTMLResponse)
def setup_page(_=Depends(auth)):
    status = (f"Logged in as <b>{escape(spotify.username or '?')}</b>" if spotify.has_credentials
              else "Not logged in")
    url = spotify.begin_oauth()
    return f"""<!doctype html><meta charset=utf-8><title>Spotify Jam Bridge</title>
<style>body{{font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 1rem;color:#222}}
input{{width:100%;padding:.5rem}}button{{padding:.5rem 1rem}}code{{background:#eee;padding:2px 4px}}</style>
<h1>Spotify Jam Bridge</h1><p>{status}</p>
<h2>Log in</h2>
<ol>
<li><a href="{escape(url)}" target="_blank">Open the Spotify login page</a> and approve access.</li>
<li>Your browser is then sent to <code>http://127.0.0.1:5588/login?code=…</code>. That page will fail to load. Copy the full address from the address bar.</li>
<li>Paste it here:</li>
</ol>
<form method=post action="/setup{'?key=' + API_KEY if API_KEY else ''}">
<input name=code placeholder="http://127.0.0.1:5588/login?code=AQ..." required>
<p><button>Finish login</button></p></form>
<p><a href="/jam{'?key=' + API_KEY if API_KEY else ''}">Current Jam (JSON)</a> ·
<a href="/jam/qr.png{'?key=' + API_KEY if API_KEY else ''}">QR</a></p>
<form method=post action="/logout{'?key=' + API_KEY if API_KEY else ''}"><button>Log out and forget credentials</button></form>
"""


@app.post("/setup")
def setup_finish(code: str = Form(...), _=Depends(auth)):
    try:
        spotify.finish_oauth(code)
    except Exception as e:
        raise HTTPException(400, f"OAuth failed: {e}")
    refresh(force_push=True)
    return RedirectResponse(f"/setup{'?key=' + API_KEY if API_KEY else ''}", status_code=303)


@app.post("/logout")
def logout(_=Depends(auth)):
    spotify.logout()
    state.update(JamSession(), force_push=True)
    return RedirectResponse(f"/setup{'?key=' + API_KEY if API_KEY else ''}", status_code=303)


# ---------- jam ----------
@app.get("/jam")
def jam_get(fresh: bool = False, _=Depends(auth)):
    if fresh:
        return refresh()
    return state.snapshot()


@app.post("/jam/start")
def jam_start(_=Depends(auth)):
    try:
        jam = spotify.start()
    except Exception as e:
        raise HTTPException(502, str(e))
    state.update(jam)
    return state.snapshot()


@app.post("/jam/end")
def jam_end(_=Depends(auth)):
    try:
        ended = spotify.end()
    except Exception as e:
        raise HTTPException(502, str(e))
    state.update(JamSession(active=False))
    return {"ended": ended, **state.snapshot()}


@app.post("/jam/refresh")
def jam_refresh(_=Depends(auth)):
    return refresh(force_push=True)


@app.get("/jam/qr.png")
def jam_qr_png(_=Depends(auth)):
    with state.lock:
        png = state.png
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/jam/qr.svg")
def jam_qr_svg(_=Depends(auth)):
    with state.lock:
        payload = state.jam.qr_payload
    return Response(render_svg(payload), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/setup")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"error": str(exc)}, status_code=500)
