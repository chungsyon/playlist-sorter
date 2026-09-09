"""Prompt construction, batching, and response validation.

Batching is the whole reason this fits in a free tier: 50 songs per request
turns a 2,000-song playlist into ~40 calls instead of 2,000.
"""

from __future__ import annotations

import logging

from .models import Assignment, Bucket, Track
from .providers import extract_json_array
from .router import ProviderRouter

log = logging.getLogger(__name__)

SYSTEM_RULES = """You are sorting songs into playlists by their vibe.

DESTINATION PLAYLISTS:
{buckets}

SONGS (format: index | title | artist | album | tags):
{songs}

For each song, decide which destination playlists it belongs in.

Rules:
- Judge by how the song actually sounds and feels: tempo, energy, mood, genre,
  production style, and what the lyrics are about.
- A song may belong to MULTIPLE playlists if it genuinely fits more than one.
- A song may belong to NONE. Return an empty list rather than forcing a bad fit.
- Use ONLY the exact playlist names listed above, spelled exactly as written.
- confidence: 0.0-1.0. If you do not recognise the song and are inferring only
  from its title, artist and tags, use a value below 0.5. Be honest here — a low
  score sends it to human review, which is the correct outcome.
- reason: at most 10 words explaining the call.
- Return EXACTLY one entry per song. The "idx" must match the input index.

Return a JSON array of objects with keys: idx, playlists, confidence, reason."""


def build_prompt(tracks: list[Track], buckets: list[Bucket]) -> str:
    bucket_block = "\n".join(b.as_prompt_row() for b in buckets)
    song_block = "\n".join(t.as_prompt_row(i) for i, t in enumerate(tracks))
    return SYSTEM_RULES.format(buckets=bucket_block, songs=song_block)


def classify_batch(router: ProviderRouter, tracks: list[Track],
                   buckets: list[Bucket]) -> list[Assignment]:
    """Classify one batch. Raises AllProvidersExhausted upward so the job runner
    can pause with its cursor intact."""
    if not tracks:
        return []

    prompt = build_prompt(tracks, buckets)
    raw, provider_name = router.generate_json(prompt)
    items = extract_json_array(raw)

    return _validate(items, tracks, buckets, provider_name)


def _validate(items: list[dict], tracks: list[Track], buckets: list[Bucket],
              provider_name: str) -> list[Assignment]:
    """Turn loose model output into trustworthy assignments.

    Models drift on playlist names ("work out" vs "Work Out"), so names are
    matched case-insensitively and snapped back to the user's exact spelling.
    Anything unmatched is dropped rather than silently creating a phantom bucket.
    """
    canonical = {b.name.strip().lower(): b.name for b in buckets}
    by_idx: dict[int, dict] = {}

    for item in items:
        try:
            idx = int(item.get("idx", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(tracks):
            by_idx[idx] = item

    assignments: list[Assignment] = []

    for idx, track in enumerate(tracks):
        item = by_idx.get(idx)

        if item is None:
            # Model dropped this song. Record it as unassigned with zero
            # confidence so it surfaces in review instead of vanishing.
            assignments.append(Assignment(
                video_id=track.video_id, playlists=[], confidence=0.0,
                reason="No response from model for this song.",
                provider=provider_name,
            ))
            continue

        names: list[str] = []
        for raw_name in item.get("playlists") or []:
            if not isinstance(raw_name, str):
                continue
            match = canonical.get(raw_name.strip().lower())
            if match and match not in names:
                names.append(match)
            elif not match:
                log.debug("Discarding unknown playlist name %r", raw_name)

        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        assignments.append(Assignment(
            video_id=track.video_id,
            playlists=names,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(item.get("reason", ""))[:200],
            provider=provider_name,
        ))

    return assignments


def batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
