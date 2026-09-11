"""Prompt construction, batching, and response validation.

Batching is the whole reason this fits in a free tier: 50 songs per request
turns a 2,000-song playlist into ~40 calls instead of 2,000.
"""

from __future__ import annotations

import logging

from .models import Assignment, Bucket, Track, group_names
from .providers import extract_json_array, grouped_response_schema
from .router import ProviderRouter

log = logging.getLogger(__name__)

# NOTE: these templates are part of the classification cache key (see
# models.bucket_hash). Editing one invalidates cached results automatically,
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


# Used when the destinations are organised into groups. The job changes shape:
# instead of one open question ("which of these, if any?") it becomes several
# closed ones ("which of these three?"), asked once per group.
GROUPED_RULES = """You are sorting songs into playlists.

The playlists are organised into GROUPS. Each group asks a different question
about the song — one might ask when you would play it, another what it sounds
like. The groups are independent of each other.

DESTINATION PLAYLISTS:
{buckets}

SONGS (format: index | title | artist | album | tags):
{songs}

For each song, work through the groups one at a time and answer each on its own
terms. Do not let your answer in one group decide your answer in another.

WITHIN A GROUP
- Take the groups in the order listed. For each one, choose from THAT GROUP'S
  playlists only, then move on to the next group. A pick from one group never
  counts as another group's answer.
- Every group must end up with at least one playlist. A group has no "none of
  these" option: pick the closest fit even when nothing is perfect, and let the
  confidence score carry your doubt.
- Usually exactly one playlist in a group is right. Take a second only when the
  song genuinely belongs in both — not because it is close enough, and not
  because they share a genre.
- Playlists inside a group cover neighbouring ground, and their descriptions
  usually say how they differ from one another. Use that: when one of them fits
  better, choose it alone.
- Put your strongest fit in each group first.
- A playlist can only answer for the group it is listed under. If the two
  playlists you like best sit in the same group, keep the better one and go
  back to the other group for its own answer.

OPTIONAL PLAYLISTS
- Playlists listed under NOT IN ANY GROUP are optional. Include one only when it
  clearly fits. Leaving them all out is a normal result. They go under the
  "{loose_key}" key.

JUDGING
- Go by how the song actually sounds and feels: tempo, energy, mood, genre,
  production style, and what the lyrics are about.
- Use ONLY the exact playlist names listed above, spelled exactly as written.
- confidence: 0.0-1.0, across your choices as a whole. If you do not recognise
  the song and are inferring only from its title, artist and tags, use a value
  below 0.5. Be honest — a low score sends it to human review, which is the
  correct outcome.
- reason: at most 20 words, saying briefly what earns each group's pick.
- Return EXACTLY one entry per song. The "idx" must match the input index.

"picks" is an object with one key per group, holding the playlist names you
chose from that group. Every group key must be present and non-empty.

Return a JSON array of objects with keys: idx, picks, confidence, reason."""


_OPTIONAL_KEY = "(optional)"


def optional_key(buckets: list[Bucket]) -> str:
    """Where ungrouped picks go in the grouped response. Group names are free
    text, so nudge the key aside on the off chance a user picked the same one."""
    key = _OPTIONAL_KEY
    taken = {g.lower() for g in group_names(buckets)}
    while key.lower() in taken:
        key += "_"
    return key


def rules_for(buckets: list[Bucket]) -> str:
    """The prompt template these buckets will be judged under.

    Also the value fed to bucket_hash, so a job that switches between grouped
    and ungrouped sorting cannot reuse the other one's cached answers.
    """
    return GROUPED_RULES if group_names(buckets) else SYSTEM_RULES


def _bucket_block(buckets: list[Bucket]) -> str:
    """The destination list, laid out so the grouping is visible at a glance."""
    groups = group_names(buckets)
    if not groups:
        return "\n".join(b.as_prompt_row() for b in buckets)

    lines: list[str] = []
    for name in groups:
        lines.append(f'GROUP "{name}" — every song needs at least one of these:')
        lines += [b.as_prompt_row() for b in buckets if b.group == name]
        lines.append("")

    loose = [b for b in buckets if not b.group]
    if loose:
        lines.append("NOT IN ANY GROUP — optional, only when it clearly fits:")
        lines += [b.as_prompt_row() for b in loose]

    return "\n".join(lines).strip()


def build_prompt(tracks: list[Track], buckets: list[Bucket]) -> str:
    song_block = "\n".join(t.as_prompt_row(i) for i, t in enumerate(tracks))
    return rules_for(buckets).format(
        buckets=_bucket_block(buckets), songs=song_block,
        loose_key=optional_key(buckets),
    )


def response_schema(buckets: list[Bucket]) -> dict | None:
    """None keeps the provider's default flat shape — nothing to enforce."""
    groups = group_names(buckets)
    if not groups:
        return None
    loose = optional_key(buckets) if any(not b.group for b in buckets) else None
    return grouped_response_schema(groups, loose)


def classify_batch(router: ProviderRouter, tracks: list[Track],
                   buckets: list[Bucket]) -> list[Assignment]:
    """Classify one batch. Raises AllProvidersExhausted upward so the job runner
    can pause with its cursor intact."""
    if not tracks:
        return []

    prompt = build_prompt(tracks, buckets)
    raw, provider_name = router.generate_json(prompt, response_schema(buckets))
    items = extract_json_array(raw)

    return _validate(items, tracks, buckets, provider_name)


def _raw_names(item: dict) -> list:
    """Flatten one model answer into a plain list of playlist names.

    Grouped runs answer with {"picks": {"Use": [...], "Vibe": [...]}}, ungrouped
    ones with a flat "playlists" list. We store the flat form either way — which
    group a name belongs to is a property of the bucket, not of where the model
    happened to file it, so filing it wrongly cannot fake a filled group.
    """
    picks = item.get("picks")
    if isinstance(picks, dict):
        out = []
        for value in picks.values():
            if isinstance(value, list):
                out.extend(value)
        return out
    return item.get("playlists") or []


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
        for raw_name in _raw_names(item):
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
