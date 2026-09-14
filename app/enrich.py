"""Last.fm tag enrichment.

Why this exists: there is no audio-feature signal available to us. Spotify
retired its energy/danceability/valence endpoint for new apps in Nov 2024 and
YouTube Music never had one. Last.fm's community tags are the closest free
substitute — real listeners labelling songs "chill", "workout", "melancholy" —
and they measurably help on tracks the model doesn't recognise.

Entirely optional. No LASTFM_API_KEY means the pipeline runs on metadata alone.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from . import db
from .config import settings
from .models import Track

log = logging.getLogger(__name__)

API_URL = "https://ws.audioscrobbler.com/2.0/"
REQUEST_INTERVAL = 0.5  # Last.fm allows ~2 req/sec on a free key
MAX_TAGS = 5
MIN_TAG_COUNT = 10  # below this the tag is one person's opinion, not a signal

# Marks a tag list as describing the artist rather than this particular track.
# It rides along inside the list so nothing downstream needs a second field:
# the cache, the API and the review table all keep working unchanged, and the
# classifier reads it as the plain English it looks like.
ARTIST_SCOPE = "(artist tags)"

# Tags that describe the listener's library rather than the song.
JUNK_TAGS = {
    "seen live", "favorites", "favourite songs", "favourites", "favorite songs",
    "spotify", "albums i own", "my music", "vinyl", "cd", "mp3", "music",
    "awesome", "good", "great", "best", "love", "loved", "amazing", "cool",
    "beautiful", "songs", "song", "track", "tracks", "under 2000 listeners",
    "all", "usa", "american", "british", "uk", "female vocalists",
    "male vocalists", "singer-songwriter",
}


def enrich(tracks: list[Track], on_progress: Callable[[int, int], None] | None = None,
           should_stop: Callable[[], bool] | None = None) -> None:
    """Attach tags to tracks in place, using the cache wherever possible."""
    if not tracks:
        return

    cached = db.get_cached_tags([t.video_id for t in tracks])
    for t in tracks:
        if cached.get(t.video_id):
            t.tags = cached[t.video_id]

    if not settings.has_lastfm:
        log.info("No LASTFM_API_KEY — classifying on metadata alone.")
        return

    # An empty cached result is worth one more look rather than being taken as
    # final: it may predate the artist-level fallback below, and an artist
    # nearly always has tags even when Last.fm has never heard of the track.
    # Once the fallback finds something the entry stops being empty, so the
    # retry set shrinks to genuinely unknown artists after a single run.
    pending = [t for t in tracks if not cached.get(t.video_id)]
    if not pending:
        return

    log.info("Fetching Last.fm tags for %d tracks (%d already cached).",
             len(pending), len(cached))

    fresh: dict[str, list[str]] = {}
    done = 0

    with httpx.Client(timeout=10.0) as client:
        for track in pending:
            if should_stop and should_stop():
                break

            tags = _fetch_tags(client, track)
            track.tags = tags
            fresh[track.video_id] = tags
            done += 1

            # Flush periodically so a long run doesn't lose work on a crash.
            if len(fresh) >= 50:
                db.save_tags(fresh)
                fresh = {}

            if on_progress:
                on_progress(done, len(pending))

            time.sleep(REQUEST_INTERVAL)

    if fresh:
        db.save_tags(fresh)


def _fetch_tags(client: httpx.Client, track: Track) -> list[str]:
    """Tags for one track, falling back to the artist when the track is unknown.

    The fallback is the point. Last.fm has never heard of a great many tracks —
    new releases, non-Anglophone catalogues, anything obscure — and for those
    the classifier previously saw a title and an artist and nothing else, which
    is precisely the case where it has least to go on. The artist's own tags are
    weaker evidence than the track's, but they are far better than none, and
    they are labelled as artist-level so the model can weigh them accordingly.

    Failures return [] — enrichment is best-effort and must never take down a
    sort.
    """
    artist = track.artists.split(",")[0].strip()
    if not artist or not track.title:
        return []

    tags = _top_tags(client, {
        "method": "track.gettoptags",
        "artist": artist,
        "track": _clean_title(track.title),
    }, exclude=artist.lower())
    if tags:
        return tags

    # artist.gettoptags scores 0-100 by relative popularity rather than
    # counting listeners, so the track-level threshold would reject every tag.
    tags = _top_tags(client, {
        "method": "artist.gettoptags",
        "artist": artist,
    }, exclude=artist.lower(), min_count=1)
    return [ARTIST_SCOPE] + tags if tags else []


def _top_tags(client: httpx.Client, params: dict, exclude: str,
               min_count: int = MIN_TAG_COUNT) -> list[str]:
    """One Last.fm toptags call, filtered down to tags that say something."""
    params = {
        **params,
        "api_key": settings.lastfm_api_key,
        "format": "json",
        "autocorrect": "1",
    }

    try:
        resp = client.get(API_URL, params=params)
        if resp.status_code != 200:
            return []
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.debug("Last.fm lookup failed for %s: %s", params.get("artist"), e)
        return []

    raw = payload.get("toptags", {}).get("tag", [])
    if isinstance(raw, dict):
        raw = [raw]

    out: list[str] = []

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        try:
            count = int(entry.get("count", 0))
        except (TypeError, ValueError):
            count = 0

        lowered = name.lower()
        if (not name or count < min_count
                or lowered in JUNK_TAGS or lowered == exclude):
            continue

        out.append(name.lower())
        if len(out) >= MAX_TAGS:
            break

    return out


def _clean_title(title: str) -> str:
    """Strip the decoration YouTube Music titles carry, which Last.fm won't match."""
    for marker in (" (Official", " [Official", " (Lyric", " (Audio", " (Video",
                   " (Music Video", " (Visualizer", " (feat.", " [feat."):
        idx = title.find(marker)
        if idx > 0:
            title = title[:idx]
    return title.strip()
