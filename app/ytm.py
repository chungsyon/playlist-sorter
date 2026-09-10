"""YouTube Music access via ytmusicapi.

This is an unofficial client (see PLAN.md risks). The official Data API charges
50 quota units per playlist insert against a 10,000/day allowance = 200 songs
per day, which is unusable here. In exchange for that, everything in this module
is deliberately gentle: writes are chunked, paced, and diffed against what is
already in the destination so we never make a redundant call.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ytmusicapi
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.auth.oauth import RefreshingToken

from .config import settings
from .models import Track

log = logging.getLogger(__name__)

_client: YTMusic | None = None


class NotAuthenticated(RuntimeError):
    pass


class WriteFailed(RuntimeError):
    pass


class OAuthPending(RuntimeError):
    """The user has not finished the consent screen yet. Keep polling."""


# Headers worth forwarding. Everything else is either irrelevant or actively
# harmful — notably content-encoding/content-length, which describe the body of
# the request you copied and break every request we make if carried over.
KEEP_HEADERS = {
    "cookie", "authorization", "x-goog-authuser", "x-origin", "origin",
    "user-agent", "accept", "accept-language", "content-type",
    # Your real visitor id. Keep it: when it is absent ytmusicapi negotiates a
    # fresh anonymous one per client, which does not carry your session.
    "x-goog-visitor-id",
}

# Deliberately NOT forwarded, even though DevTools shows them.
#
# x-youtube-client-name / x-youtube-client-version pin the request to whatever
# YouTube Music build your browser is running. Current builds answer with the
# `singleColumnBrowseResultsRenderer` layout, which ytmusicapi cannot parse. The
# symptom is an empty library plus a KeyError naming
# `twoColumnBrowseResultsRenderer` on calls like get_playlist("LM"). Omitting
# them lets ytmusicapi negotiate a client version it understands — this was
# reproducible: dropping these two turned an empty library into all 17
# playlists.
#
# Worth knowing: x-goog-pageid is the one header in this family you may actually
# need — brand accounts require it. Add it to KEEP_HEADERS if your music library
# lives under a secondary channel.
DROP_HEADERS = {"x-youtube-client-name", "x-youtube-client-version"}

_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


def setup_auth(headers_raw: str) -> None:
    """Write browser.json from request headers pasted out of DevTools.

    Accepts both formats browsers produce: the "Raw" view (`name: value`) and
    Chrome's default view, which puts the name and value on separate lines.
    """
    if not headers_raw.strip():
        raise ValueError("No headers provided.")

    headers = parse_headers(headers_raw)

    if "cookie" not in headers:
        raise ValueError(
            "No Cookie header found. Copy the full Request Headers block from an "
            "authenticated POST to /browse on music.youtube.com — the Cookie line "
            "is the one that matters."
        )
    if "authorization" not in headers:
        raise ValueError(
            "No Authorization header found. That request was probably "
            "unauthenticated — pick a POST request to /browse instead."
        )

    # Defensive: keeps these out even if someone widens KEEP_HEADERS later.
    for key in DROP_HEADERS:
        headers.pop(key, None)

    # ytmusicapi requires this; 0 is the correct value for a single-account login.
    headers.setdefault("x-goog-authuser", "0")

    normalized = "\n".join(f"{k}: {v}" for k, v in headers.items())
    ytmusicapi.setup(filepath=str(settings.ytm_auth_file), headers_raw=normalized)
    reset_client()


def parse_headers(raw: str) -> dict[str, str]:
    """Parse pasted DevTools headers into a clean dict.

    Handles, in order of nastiness:
      * `name: value` on one line (the "Raw" toggle / Firefox)
      * name and value on separate lines (Chrome's default view)
      * HTTP/2 pseudo-headers (`:authority`, `:method`, ...) whose values would
        otherwise be mistaken for the next header's name
      * the protobuf block Chrome prints under `x-client-data` as "Decoded:"
    """
    headers: dict[str, str] = {}
    pending: str | None = None
    in_decoded_block = False

    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue

        if text.lower().startswith("decoded:"):
            in_decoded_block = True
            continue
        if in_decoded_block:
            if text == "}":
                in_decoded_block = False
            continue

        # Pseudo-header: drop it, and drop whatever follows it on the next line.
        if text.startswith(":"):
            pending = None if ": " in text else "\x00skip"
            continue

        # One-line "name: value".
        if ": " in text:
            name, _, value = text.partition(": ")
            if _HEADER_NAME.match(name):
                key = name.lower()
                if key in KEEP_HEADERS:
                    headers[key] = value.strip()
                pending = None
                continue

        # Two-line form: a bare header name, then its value on the next line.
        if pending is None:
            candidate = text.rstrip(":").lower()
            pending = candidate if _HEADER_NAME.match(candidate) else "\x00skip"
        else:
            if pending in KEEP_HEADERS:
                headers[pending] = text
            pending = None

    return headers


# --------------------------------------------------------------------------
# OAuth (the recommended path)
#
# ytmusicapi's own setup_oauth() blocks on input(), so the two halves of the
# device flow are driven here instead: get_code() hands back a short user code,
# and token_from_code() is polled until the user finishes consenting.
#
# This still talks to YouTube Music's internal endpoints, so there is no
# Data API quota ceiling — the OAuth client only supplies the credentials.
# --------------------------------------------------------------------------

def oauth_credentials() -> OAuthCredentials:
    if not settings.has_oauth_client:
        raise NotAuthenticated(
            "No OAuth client configured. Add a client ID and secret on the "
            "Setup screen — see the README for how to create one."
        )
    return OAuthCredentials(
        client_id=settings.ytm_oauth_client_id,
        client_secret=settings.ytm_oauth_client_secret,
    )


def oauth_start() -> dict[str, Any]:
    """Begin the device flow. Returns the code and URL to show the user."""
    code = oauth_credentials().get_code()
    return {
        "device_code": code["device_code"],
        "user_code": code["user_code"],
        "verification_url": code["verification_url"],
        "full_url": f"{code['verification_url']}?user_code={code['user_code']}",
        "interval": int(code.get("interval", 5)),
        "expires_in": int(code.get("expires_in", 1800)),
    }


def oauth_finish(device_code: str) -> None:
    """Exchange the device code for a token, or raise OAuthPending.

    Google answers with an `error` field rather than an HTTP error while the
    user is still on the consent screen, so the body has to be inspected.
    """
    creds = oauth_credentials()
    raw = creds.token_from_code(device_code)

    error = raw.get("error") if isinstance(raw, dict) else None
    if error in {"authorization_pending", "slow_down"}:
        raise OAuthPending(error)
    if error == "expired_token":
        raise NotAuthenticated("That sign-in code expired. Start again.")
    if error == "access_denied":
        raise NotAuthenticated("Sign-in was declined.")
    if error:
        raise NotAuthenticated(f"Sign-in failed: {error}")

    expires = raw.get("refresh_token_expires_in", raw["expires_in"])
    token = RefreshingToken(
        credentials=creds,
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        scope=raw["scope"],
        token_type=raw["token_type"],
        expires_in=expires,
    )
    token.update(raw)
    token.local_cache = settings.ytm_oauth_file  # setter writes the file
    reset_client()


def auth_mode() -> str:
    """Which credential file is in play: 'oauth', 'browser', or ''."""
    if settings.ytm_oauth_file.exists():
        return "oauth"
    if settings.ytm_auth_file.exists():
        return "browser"
    return ""


def disconnect() -> None:
    """Forget the stored session. Used when signing out or switching accounts."""
    for path in (settings.ytm_oauth_file, settings.ytm_auth_file):
        path.unlink(missing_ok=True)
    reset_client()


def reset_client() -> None:
    global _client
    _client = None


def get_client() -> YTMusic:
    """One cached client for the process.

    Caching matters beyond speed: each new client replays the stored session,
    and rapid re-authentication is what got a session dropped during earlier
    debugging.
    """
    global _client
    if _client is None:
        mode = auth_mode()
        if mode == "oauth":
            _client = YTMusic(
                str(settings.ytm_oauth_file),
                oauth_credentials=oauth_credentials(),
            )
        elif mode == "browser":
            _client = YTMusic(str(settings.ytm_auth_file))
        else:
            raise NotAuthenticated(
                "Not connected to YouTube Music. Sign in on the Connect screen."
            )
    return _client


def check_auth() -> tuple[bool, str]:
    """Confirm the session works *and* that we can actually read the library.

    Checking only that a call doesn't raise is not enough: a wrong client
    version returns a layout ytmusicapi parses into an empty list, so the call
    "succeeds" while showing you nothing. Treat an empty library as a failure.
    """
    try:
        yt = get_client()
    except NotAuthenticated as e:
        return False, str(e)

    account = ""
    try:
        account = (yt.get_account_info() or {}).get("accountName", "")
    except Exception:  # noqa: BLE001 - non-fatal, it's only for the message
        pass

    try:
        playlists = yt.get_library_playlists(limit=None) or []
    except Exception as e:  # noqa: BLE001 - the raw reason is the useful part
        return False, f"Auth check failed: {e}"

    if not playlists:
        again = ("Sign in again on the Connect screen." if auth_mode() == "oauth"
                 else "Capture fresh request headers and reconnect.")
        return False, (
            "YouTube answered as if signed out — no playlists visible. Your "
            f"session has most likely expired. {again}"
        )

    who = f" as {account}" if account else ""
    return True, f"Connected{who} — {len(playlists)} playlists."


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def list_playlists() -> list[dict[str, Any]]:
    """Every playlist in the library, plus Liked Music, for the UI dropdowns."""
    yt = get_client()
    out: list[dict[str, Any]] = []
    for p in yt.get_library_playlists(limit=None) or []:
        out.append({
            "playlist_id": p.get("playlistId", ""),
            "name": p.get("title", "(untitled)"),
            "count": _coerce_count(p.get("count")),
        })
    out.append({"playlist_id": "LM", "name": "Liked Music", "count": None})
    return [p for p in out if p["playlist_id"]]


def fetch_all_tracks(playlist_id: str) -> tuple[str, list[Track]]:
    """Full playlist, no size cap.

    `limit=None` makes ytmusicapi paginate until exhausted — this is what
    satisfies the "whole playlist, no restrictions" requirement.
    """
    yt = get_client()
    data = yt.get_playlist(playlist_id, limit=None)
    name = data.get("title", "")
    tracks: list[Track] = []
    seen: set[str] = set()

    for item in data.get("tracks", []) or []:
        vid = item.get("videoId")
        if not vid or vid in seen:
            continue  # unavailable/removed entries come back with no videoId
        seen.add(vid)
        tracks.append(Track(
            video_id=vid,
            title=item.get("title", "") or "",
            artists=_join_artists(item.get("artists")),
            album=_album_name(item.get("album")),
            duration_seconds=int(item.get("duration_seconds") or 0),
            is_available=bool(item.get("isAvailable", True)),
        ))
    return name, tracks


def get_playlist_video_ids(playlist_id: str) -> set[str]:
    """Existing contents of a destination, so Apply never re-adds a song."""
    try:
        _, tracks = fetch_all_tracks(playlist_id)
        return {t.video_id for t in tracks}
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read destination %s: %s", playlist_id, e)
        return set()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def add_tracks(playlist_id: str, video_ids: list[str],
               on_progress=None) -> tuple[int, list[str]]:
    """Add songs in paced chunks. Returns (added_count, failed_ids).

    The sleep between chunks is not politeness theatre — rapid-fire writes
    through an unofficial client are what gets accounts flagged.
    """
    if not video_ids:
        return 0, []

    yt = get_client()
    added = 0
    failed: list[str] = []
    chunk_size = max(1, settings.ytm_write_chunk_size)

    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i:i + chunk_size]
        try:
            resp = yt.add_playlist_items(playlist_id, videoIds=chunk, duplicates=False)
            if _write_ok(resp):
                added += len(chunk)
            else:
                log.warning("Write rejected for %s: %s", playlist_id, resp)
                failed.extend(chunk)
        except Exception as e:  # noqa: BLE001
            log.warning("Write failed for %s: %s", playlist_id, e)
            failed.extend(chunk)

        if on_progress:
            on_progress(added, len(failed))

        if i + chunk_size < len(video_ids):
            time.sleep(settings.ytm_write_delay_seconds)

    return added, failed


def _write_ok(resp: Any) -> bool:
    if resp is None:
        return False
    if isinstance(resp, str):
        return "STATUS_SUCCEEDED" in resp.upper() or resp != ""
    if isinstance(resp, dict):
        status = str(resp.get("status", "")).upper()
        return "SUCCEEDED" in status or bool(resp.get("playlistEditResults"))
    return True


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------

def snapshot_playlists(playlist_ids: list[str], label: str) -> Path:
    """Dump affected playlists to disk before any write.

    We only ever add songs, never remove, so this is belt-and-braces — but if a
    write ever goes somewhere unintended, this file is what makes it reversible.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = settings.backup_dir / f"{stamp}-{label}.json"
    payload: dict[str, Any] = {"created_at": stamp, "playlists": {}}

    for pid in playlist_ids:
        try:
            name, tracks = fetch_all_tracks(pid)
            payload["playlists"][pid] = {
                "name": name,
                "video_ids": [t.video_id for t in tracks],
                "tracks": [{"video_id": t.video_id, "title": t.title,
                            "artists": t.artists} for t in tracks],
            }
        except Exception as e:  # noqa: BLE001
            payload["playlists"][pid] = {"error": str(e)}

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _join_artists(artists: Any) -> str:
    if not artists:
        return ""
    if isinstance(artists, str):
        return artists
    names = [a.get("name", "") for a in artists if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


def _album_name(album: Any) -> str:
    if not album:
        return ""
    if isinstance(album, str):
        return album
    if isinstance(album, dict):
        return album.get("name", "") or ""
    return ""


def _coerce_count(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None
