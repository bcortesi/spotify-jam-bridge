"""
Spotify session management and Jam (social-connect) calls.

Uses librespot-python for a headless, OAuth-authenticated Spotify session and
talks to the same internal spclient endpoints the official clients use.
These endpoints are undocumented and may change without notice.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from librespot.core import Session
from librespot.mercury import MercuryRequests
from librespot.oauth import OAuth
from librespot.proto import Connect_pb2 as Connect
from requests.structures import CaseInsensitiveDict

log = logging.getLogger("jam.spotify")

OAUTH_REDIRECT = "http://127.0.0.1:5588/login"  # fixed by Spotify for this client id


@dataclass
class JamSession:
    active: bool = False
    session_id: Optional[str] = None
    join_token: Optional[str] = None
    join_uri: Optional[str] = None
    join_url: Optional[str] = None
    short_url: Optional[str] = None
    owner: Optional[str] = None
    member_count: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def qr_payload(self) -> Optional[str]:
        return self.short_url or self.join_url

    def key(self) -> tuple:
        return (self.active, self.session_id, self.join_token)

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "session_id": self.session_id,
            "join_token": self.join_token,
            "join_uri": self.join_uri,
            "join_url": self.join_url,
            "short_url": self.short_url,
            "qr_payload": self.qr_payload,
            "owner": self.owner,
            "member_count": self.member_count,
        }


class SpotifyJam:
    def __init__(self, data_dir: str, device_name: str, user_agent: str,
                 start_query: str, use_short_link: bool):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.cred_file = os.path.join(data_dir, "credentials.json")
        self.device_id = self._load_device_id()
        self.device_name = device_name
        self.user_agent = user_agent
        self.start_query = start_query
        self.use_short_link = use_short_link
        self._session: Optional[Session] = None
        self._oauth: Optional[OAuth] = None
        self._lock = threading.RLock()

        self.conf = (Session.Configuration.Builder()
                     .set_cache_enabled(False)
                     .set_store_credentials(True)
                     .set_stored_credential_file(self.cred_file)
                     .build())

    # ---------- device id ----------
    def _load_device_id(self) -> str:
        path = os.path.join(self.data_dir, "device_id")
        if os.path.isfile(path):
            return open(path).read().strip()
        did = secrets.token_hex(20)  # 40 hex chars, as Spotify expects
        with open(path, "w") as f:
            f.write(did)
        return did

    # ---------- auth ----------
    @property
    def has_credentials(self) -> bool:
        return os.path.isfile(self.cred_file)

    @property
    def connected(self) -> bool:
        return self._session is not None and self._session.is_valid()

    @property
    def username(self) -> Optional[str]:
        try:
            return self._session.username() if self._session else None
        except Exception:
            return None

    def _builder(self) -> Session.Builder:
        return (Session.Builder(self.conf)
                .set_device_id(self.device_id)
                .set_device_name(self.device_name)
                .set_device_type(Connect.DeviceType.SPEAKER))

    def connect(self) -> None:
        """Open a session from stored credentials."""
        with self._lock:
            if not self.has_credentials:
                raise RuntimeError("No stored credentials; complete OAuth at /setup")
            self._close()
            log.info("Connecting to Spotify with stored credentials")
            self._session = self._builder().stored_file(self.cred_file).create()
            log.info("Connected as %s (device %s)", self.username, self.device_id)

    def _close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def begin_oauth(self) -> str:
        """Return the URL the user must open in a browser."""
        with self._lock:
            self._oauth = OAuth(MercuryRequests.keymaster_client_id, OAUTH_REDIRECT, None)
            return self._oauth.get_auth_url()

    def finish_oauth(self, code_or_url: str) -> None:
        """Accept either the raw ?code= value or the full redirect URL."""
        with self._lock:
            if self._oauth is None:
                raise RuntimeError("Start the OAuth flow first")
            code = code_or_url.strip()
            if code.startswith("http"):
                qs = parse_qs(urlparse(code).query)
                if "code" not in qs:
                    raise ValueError("Redirect URL contains no code parameter")
                code = qs["code"][0]
            self._oauth.set_code(code)
            self._oauth.request_token()
            creds = self._oauth.get_credentials()
            self._close()
            builder = self._builder()
            builder.login_credentials = creds
            self._session = builder.create()  # stores credentials.json
            self._oauth = None
            log.info("OAuth complete, logged in as %s", self.username)

    def logout(self) -> None:
        with self._lock:
            self._close()
            if os.path.isfile(self.cred_file):
                os.remove(self.cred_file)

    # ---------- spclient ----------
    def _api(self, method: str, path: str, body: Optional[dict] = None):
        if not self.connected:
            self.connect()
        headers = CaseInsensitiveDict({
            "Accept": "application/json",
            "App-Platform": "iOS",
            "User-Agent": self.user_agent,
        })
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        resp = self._session.api().send(method, path, headers, data)
        if resp.status_code in (401, 403):
            log.warning("%s %s -> %s, rebuilding session and retrying once", method, path, resp.status_code)
            self.connect()
            resp = self._session.api().send(method, path, headers, data)
        log.debug("%s %s -> %s", method, path, resp.status_code)
        return resp

    def _parse(self, payload: dict) -> JamSession:
        token = payload.get("join_session_token")
        js = JamSession(
            active=bool(payload.get("active", bool(token))),
            session_id=payload.get("session_id"),
            join_token=token,
            join_uri=payload.get("join_session_uri"),
            join_url=f"https://open.spotify.com/socialsession/{token}" if token else None,
            owner=payload.get("session_owner_id"),
            member_count=len(payload.get("session_members") or []),
            raw=payload,
        )
        if js.active and self.use_short_link and js.join_uri:
            js.short_url = self._short_link(js.join_uri)
        return js

    def current(self) -> JamSession:
        with self._lock:
            resp = self._api("GET", "/social-connect/v2/sessions/current?alt=json")
            if resp.status_code == 404 or not resp.content:
                return JamSession(active=False)
            if resp.status_code != 200:
                raise RuntimeError(f"current: HTTP {resp.status_code} {resp.text[:200]}")
            return self._parse(resp.json())

    def start(self) -> JamSession:
        with self._lock:
            resp = self._api("GET", f"/social-connect/v2/sessions/current_or_new?{self.start_query}")
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"start: HTTP {resp.status_code} {resp.text[:200]}")
            return self._parse(resp.json())

    def end(self) -> bool:
        with self._lock:
            cur = self.current()
            if not cur.session_id:
                return False
            resp = self._api("DELETE", f"/social-connect/v3/sessions/{cur.session_id}")
            if resp.status_code not in (200, 204):
                raise RuntimeError(f"end: HTTP {resp.status_code} {resp.text[:200]}")
            return True

    def _short_link(self, join_uri: str) -> Optional[str]:
        """Ask Spotify's url-dispenser for a spotify.link short URL (what the app's QR encodes)."""
        body = {
            "custom_data": [
                {"key": "jam", "value": "1"},
                {"key": "ipl", "value": "1"},
                {"key": "app_destination", "value": "socialsession"},
                {"key": "ssp", "value": "1"},
            ],
            "link_preview": {"title": "Join my Jam on Spotify"},
            "utm_parameters": {"utm_medium": "share-link", "utm_source": "share-options-sheet"},
            "spotify_uri": join_uri,
        }
        try:
            resp = self._api("POST", "/url-dispenser/v1/generate-url", body)
            if resp.status_code in (200, 201):
                return resp.json().get("shareable_url")
            log.warning("url-dispenser HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:  # short link is optional
            log.warning("url-dispenser failed: %s", e)
        return None
