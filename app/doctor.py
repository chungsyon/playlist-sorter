"""Preflight check — confirms every piece is wired up before you start a sort.

    uv run python -m app.doctor
"""

from __future__ import annotations

import shutil
import sys

from .config import settings
from .providers import ClaudeCLIProvider, GeminiProvider, ProviderError, extract_json_array

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "

PROBE = ('Return a JSON array with exactly one object: '
         '{"idx": 0, "playlists": ["test"], "confidence": 1.0, "reason": "probe"}')


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f"  — {detail}" if detail else ""))


def check_gemini() -> bool:
    if not settings.has_gemini:
        line(BAD, "Gemini", "GEMINI_API_KEY missing. https://aistudio.google.com/apikey")
        return False
    try:
        items = extract_json_array(GeminiProvider().generate_json(PROBE))
        line(OK, "Gemini", f"{settings.gemini_model} responded ({len(items)} item)")
        return True
    except ProviderError as e:
        line(BAD, "Gemini", str(e)[:160])
        return False


def check_claude() -> bool:
    provider = ClaudeCLIProvider()
    if not provider.available():
        line(WARN, "Claude fallback",
             f"`{settings.claude_cli_path}` not on PATH — no fallback when Gemini runs dry")
        return False
    try:
        items = extract_json_array(provider.generate_json(PROBE))
        line(OK, "Claude fallback", f"{settings.claude_model} responded ({len(items)} item)")
        return True
    except ProviderError as e:
        line(WARN, "Claude fallback", str(e)[:160])
        return False


def check_ytmusic() -> bool:
    from . import ytm
    if not settings.ytm_authenticated:
        line(WARN, "YouTube Music",
             f"{settings.ytm_auth_file.name} not found — connect in the web UI first")
        return False
    ok, message = ytm.check_auth()
    if not ok:
        line(BAD, "YouTube Music", message[:160])
        return False
    try:
        count = len(ytm.list_playlists())
        line(OK, "YouTube Music", f"connected, {count} playlists visible")
    except Exception as e:  # noqa: BLE001
        line(WARN, "YouTube Music", f"connected but listing failed: {e}")
    return True


def check_lastfm() -> bool:
    if not settings.has_lastfm:
        line(WARN, "Last.fm", "no key — classifying on metadata alone (works, less accurate)")
        return False
    import httpx
    try:
        r = httpx.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "track.gettoptags", "artist": "Daft Punk",
                    "track": "Around the World", "api_key": settings.lastfm_api_key,
                    "format": "json"},
            timeout=10.0,
        )
        if r.status_code == 200 and "toptags" in r.json():
            line(OK, "Last.fm", "tag enrichment enabled")
            return True
        line(WARN, "Last.fm", f"unexpected response ({r.status_code})")
    except Exception as e:  # noqa: BLE001
        line(WARN, "Last.fm", str(e)[:160])
    return False


def main() -> int:
    print("\nPlaylist Sorter — preflight\n" + "-" * 52)

    gemini_ok = check_gemini()
    claude_ok = check_claude()
    check_lastfm()
    ytm_ok = check_ytmusic()

    print("-" * 52)

    if not (gemini_ok or claude_ok):
        print("\nBlocked: no working classifier. Fix Gemini or the Claude CLI.\n")
        return 1
    if not ytm_ok:
        print("\nAlmost there: start the app and connect YouTube Music on the "
              "first screen.\n")
        return 0

    print("\nAll set. Start it with:  uv run python -m app.main\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
