"""
Minimal, polite client for BandLab's private web API (api/v1.3).

Facts this module relies on were verified against the public feed and the
BandLab web bundle (web-app/scripts, version 10.2.86):

  * Base URL ............ https://www.bandlab.com/api/v1.3
  * Auth ................ Bearer JWT. Renewable from the browser "refreshToken"
                          cookie via accounts.bandlab.com OAuth (client "bandlab_web").
  * Songs ............... GET /users/{userId}/songs  (cursor pagination, ?after=)
  * Revision ............ GET /revisions/{revisionId}  -> tracks[], regions[], effects[],
                          samples[], metronome, key, mastering, mixdown
  * Mixdown (server) .... revision.mixdown.file  -> static CDN .m4a (lossy)
  * Mixdown (download) .. POST /revisions/{id}/mixdown/download-options/{optionId}
                          then GET the same URL until {"url": ...}  (optionId e.g.
                          "wav-44100-16bit", "aac-44100-16bit-192kbps")
  * Track stem .......... POST /revisions/{id}/tracks/{trackId}/download-options/{optionId}
                          then GET the same URL until {"url": ...}   (server-rendered)
  * Original sample ..... https://static.bandlab.com/samples-original/{sampleId}/{sampleId}.wav
                          (the raw recording as uploaded/recorded, WAV)
  * Formatted sample .... https://static.bandlab.com/samples-formatted/{sampleId}/{sampleId}.m4a

Nothing here writes to BandLab except the download-option POSTs, which only ask
the server to render a file. No revision is ever created, modified or deleted.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import requests

log = logging.getLogger("bl2fl.api")

API_BASE = "https://www.bandlab.com/api/v1.3"
OAUTH_TOKEN_URL = "https://accounts.bandlab.com/oauth/connect/token"
OAUTH_CLIENT_ID = "bandlab_web"
STATIC_BASE = "https://static.bandlab.com"
WEB_CLIENT_VERSION = "10.2.86"

STEM_OPTION_WAV = "wav-44100-16bit"
STEM_OPTION_M4A = "aac-44100-16bit-192kbps"


class BandLabError(RuntimeError):
    pass


class AuthError(BandLabError):
    pass


class ApiError(BandLabError):
    def __init__(self, message: str, status: int, body: str):
        super().__init__(message)
        self.status = status
        self.body = body


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


@dataclass
class Session:
    """Holds the bearer token and (optionally) a self-renewing refresh token.

    The password is never accepted here on purpose: log in with the browser
    and paste either the ``refreshToken`` cookie or an ``Authorization`` header.
    """

    state_file: Path
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: float = 0.0

    @classmethod
    def load(cls, state_file: Path, env: Optional[dict] = None) -> "Session":
        env = env or os.environ
        s = cls(state_file=state_file)
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text("utf-8"))
                s.access_token = data.get("access_token")
                s.refresh_token = data.get("refresh_token")
                s.expires_at = float(data.get("expires_at", 0))
            except (OSError, ValueError):
                log.warning("Could not read %s; starting a fresh session", state_file)
        # Environment overrides (what the user pasted most recently wins).
        rt = (env.get("BANDLAB_REFRESH_TOKEN") or "").strip()
        sk = (env.get("BANDLAB_SESSION_KEY") or "").strip()
        if sk.lower().startswith("bearer "):
            sk = sk[7:].strip()
        if rt and rt != s.refresh_token:
            s.refresh_token = rt
            s.access_token = None
            s.expires_at = 0
        if sk and sk != s.access_token:
            s.access_token = sk
            # A pasted header has no known expiry; try it until a 401 says otherwise.
            s.expires_at = time.time() + 23 * 3600
        return s

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at,
                }
            ),
            "utf-8",
        )
        _chmod_private(self.state_file)

    def has_credentials(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def token(self, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self.access_token
            and time.time() < self.expires_at - 60
        ):
            return self.access_token
        if self.refresh_token:
            self._refresh()
            return self.access_token  # type: ignore[return-value]
        if self.access_token and not force_refresh:
            return self.access_token
        raise AuthError(
            "No usable BandLab credentials. Log in at bandlab.com in your browser, then "
            "set BANDLAB_REFRESH_TOKEN (DevTools > Application > Cookies > refreshToken) "
            "or BANDLAB_SESSION_KEY (the Authorization header of any api/v1.3 request)."
        )

    def _refresh(self) -> None:
        log.info("Renewing BandLab access token via OAuth refresh")
        r = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": self.refresh_token,
            },
            headers={"user-agent": _UA},
            timeout=60,
        )
        if r.status_code != 200:
            raise AuthError(
                f"OAuth refresh failed (HTTP {r.status_code}): {r.text[:300]}. "
                "Paste a fresh refreshToken cookie from a logged-in browser."
            )
        data = r.json()
        self.access_token = data.get("access_token")
        if not self.access_token:
            raise AuthError("OAuth refresh returned no access_token")
        # BandLab rotates refresh tokens; keep the new one or the session dies.
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.expires_at = time.time() + float(data.get("expires_in", 3600))
        self.save()


_UA = f"Mozilla/5.0 (compatible; bl2fl/0.1; +migration tool; web-app {WEB_CLIENT_VERSION})"


class BandLabClient:
    """Serialized, rate-limited HTTP client. Read-only except for render requests."""

    def __init__(
        self,
        session: Session,
        min_interval_s: float = 1.2,
        api_base: str = API_BASE,
        on_progress: Optional[Callable[[str], None]] = None,
    ):
        self.session = session
        self.min_interval_s = min_interval_s
        self.api_base = api_base.rstrip("/")
        self._last_call = 0.0
        self._http = requests.Session()
        self._http.headers.update({"user-agent": _UA, "accept": "application/json"})
        self.on_progress = on_progress or (lambda msg: None)

    # ------------------------------------------------------------------ core
    def _throttle(self) -> None:
        wait = self._last_call + self.min_interval_s - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Any = None,
        retries: int = 4,
        absolute: bool = False,
    ) -> Any:
        url = path if absolute else f"{self.api_base}{path}"
        attempt = 0
        refreshed = False
        while True:
            self._throttle()
            headers = {"authorization": f"Bearer {self.session.token()}"}
            if json_body is not None:
                headers["content-type"] = "application/json"
            try:
                r = self._http.request(
                    method, url, params=params, json=json_body, headers=headers, timeout=90
                )
            except requests.RequestException as e:
                attempt += 1
                if attempt > retries:
                    raise BandLabError(f"{method} {url} failed: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401 and not refreshed and self.session.refresh_token:
                refreshed = True
                self.session.token(force_refresh=True)
                continue
            if r.status_code == 401:
                raise AuthError(
                    "BandLab rejected the token (HTTP 401). It has probably expired; paste a "
                    "fresh refreshToken cookie or Authorization header."
                )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                backoff = float(r.headers.get("retry-after") or 2 ** attempt)
                log.warning("HTTP %s from %s; retrying in %.0fs", r.status_code, url, backoff)
                time.sleep(backoff)
                continue
            if r.status_code >= 400:
                raise ApiError(
                    f"{method} {url} -> HTTP {r.status_code}", r.status_code, r.text[:600]
                )
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: Any = None) -> Any:
        return self.request("POST", path, json_body=json_body)

    def paginate(self, path: str, params: Optional[dict] = None, page_size: int = 20) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("limit", page_size)
        after = None
        seen: set = set()
        while True:
            if after:
                params["after"] = after
            page = self.get(path, params)
            if isinstance(page, list):
                items, paging = page, {}
            else:
                items = page.get("data") or []
                paging = page.get("paging") or {}
            for it in items:
                key = it.get("id") if isinstance(it, dict) else None
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                yield it
            cursors = paging.get("cursors") or {}
            after = cursors.get("after") or paging.get("nextCursor")
            if not after or not items:
                break

    # --------------------------------------------------------------- account
    def me(self) -> dict:
        return self.get("/me")

    def list_my_songs(self, user_id: str) -> list[dict]:
        """Every song (BandLab 'project') the user owns or collaborates on."""
        songs: dict[str, dict] = {}
        for s in self.paginate(f"/users/{user_id}/songs"):
            if s.get("id"):
                songs[s["id"]] = s
        return list(songs.values())

    def get_song(self, song_id: str) -> dict:
        return self.get(f"/songs/{song_id}")

    def list_revisions(self, song_id: str) -> list[dict]:
        return list(self.paginate(f"/songs/{song_id}/revisions"))

    def get_revision(self, revision_id: str) -> dict:
        return self.get(f"/revisions/{revision_id}")

    # ------------------------------------------------------------- downloads
    def request_render(
        self,
        revision_id: str,
        option_id: str,
        track_id: Optional[str] = None,
        timeout_s: float = 900,
        poll_interval_s: float = 5.0,
    ) -> str:
        """Ask BandLab to render a mixdown (track_id=None) or one track stem and
        return the resulting file URL. Mirrors the web app: POST, then GET the
        same URL until the response carries ``url``."""
        if track_id:
            path = f"/revisions/{revision_id}/tracks/{track_id}/download-options/{option_id}"
        else:
            path = f"/revisions/{revision_id}/mixdown/download-options/{option_id}"
        first = self.post(path)
        if isinstance(first, dict) and first.get("url"):
            return first["url"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(poll_interval_s)
            res = self.get(path)
            if isinstance(res, dict) and res.get("url"):
                return res["url"]
            status = (res or {}).get("status") if isinstance(res, dict) else None
            self.on_progress(f"  waiting for BandLab render ({option_id}) status={status}")
            if status and str(status).lower() in ("failed", "error", "corrupted"):
                raise BandLabError(f"BandLab render failed for {path}: {res}")
        raise BandLabError(f"Timed out waiting for BandLab render: {path}")

    def list_mixdown_options(self, revision_id: str) -> list[dict]:
        try:
            res = self.get(f"/revisions/{revision_id}/mixdown/download-options")
            if isinstance(res, dict):
                res = res.get("data") or res.get("options") or []
            return res if isinstance(res, list) else []
        except ApiError:
            return []

    def download(
        self,
        url: str,
        dest: Path,
        expected_min_bytes: int = 1,
        overwrite: bool = False,
        auth: bool = False,
    ) -> Path:
        """Stream a file to ``dest`` atomically. Skips when it already exists."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite and dest.stat().st_size >= expected_min_bytes:
            return dest
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {"user-agent": _UA}
        if auth:
            headers["authorization"] = f"Bearer {self.session.token()}"
        for attempt in range(5):
            try:
                with self._http.get(url, headers=headers, stream=True, timeout=120) as r:
                    if r.status_code == 404:
                        raise ApiError(f"GET {url} -> 404", 404, "")
                    r.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            if chunk:
                                fh.write(chunk)
                size = tmp.stat().st_size
                if size < expected_min_bytes:
                    raise BandLabError(f"Downloaded file too small ({size} B): {url}")
                os.replace(tmp, dest)
                return dest
            except ApiError:
                raise
            except (requests.RequestException, BandLabError, OSError) as e:
                if attempt == 4:
                    raise BandLabError(f"Download failed: {url}: {e}") from e
                time.sleep(2 ** (attempt + 1))
        return dest

    # --------------------------------------------------------------- static
    @staticmethod
    def sample_original_url(sample_id: str) -> str:
        return f"{STATIC_BASE}/samples-original/{sample_id}/{sample_id}.wav"

    @staticmethod
    def sample_formatted_url(sample_id: str) -> str:
        return f"{STATIC_BASE}/samples-formatted/{sample_id}/{sample_id}.m4a"

    def head_ok(self, url: str) -> bool:
        try:
            self._throttle()
            r = self._http.get(url, headers={"user-agent": _UA, "range": "bytes=0-0"}, timeout=30)
            return r.status_code in (200, 206)
        except requests.RequestException:
            return False
