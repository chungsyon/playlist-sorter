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

# NOTE: this template is part of the classification cache key (see
# models.bucket_hash). Editing it invalidates cached results automatically,
# which is intended — old results were produced under different instructions.
SYSTEM_RULES = """You are sorting songs into playlists by their vibe.

DESTINATION PLAYLISTS:
{buckets}

SONGS (format: index | title | artist | album | tags):
{songs}

For each song, decide which destination playlists it belongs in.

HOW MANY PLAYLISTS PER SONG
- Do not decide on a number first. Take each playlist in turn and ask one
  question: does this song belong here? Keep the ones where the answer is
  clearly yes.
- Judged that way, many songs land in exactly one playlist, some genuinely live
  in two, a few fit none at all, and now and then one earns three. All of those
  are normal results — let the song decide, not a quota.
- Do not include a playlist because the song is "close enough", or because they
  share a genre. Sitting next to a mood is not the same as belonging to it.
- Return an EMPTY list only when no playlist is a good home for the song. That
  is a real answer where it applies, but it is the exception.
- Your reason and your list must agree. If the reason says the song suits a
  playlist, that playlist has to appear in the list.
- Before you settle on an empty list, read back the words you used to describe
  the song and check them against the playlist descriptions once more. Where a
  description uses the same language, that playlist belongs in the list.

CHOOSING BETWEEN SIMILAR PLAYLISTS
- Several playlists may cover neighbouring moods, and their descriptions
  usually say how they differ from one another. Use that: when one of them
  clearly fits better, choose it alone. Keep both only when the song genuinely
  lives in both.
- Put the strongest fit first in the list.

JUDGING
- Go by how the song actually sounds and feels: tempo, energy, mood, genre,
  production style, and what the lyrics are about.
- Use ONLY the exact playlist names listed above, spelled exactly as written.
- confidence: 0.0-1.0, for your single best-fit choice. If you do not recognise
  the song and are inferring only from its title, artist and tags, use a value
  below 0.5. Be honest — a low score sends it to human review, which is the
  correct outcome.
- reason: at most 15 words. If you list more than one playlist, say briefly what
  earns it the second.
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
