"""Shared data shapes."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field


class Track(BaseModel):
    video_id: str
    title: str
    artists: str = ""
    album: str = ""
    duration_seconds: int = 0
    is_available: bool = True
    tags: list[str] = Field(default_factory=list)

    def as_prompt_row(self, idx: int) -> str:
        """One compact line for the classifier. Keeping this tight is what keeps
        a 2,000-song playlist inside the free tier."""
        parts = [f"{idx}", self.title, self.artists]
        if self.album:
            parts.append(self.album)
        if self.tags:
            # Six, not five: an artist-level list spends its first slot on the
            # "(artist tags)" marker, which would otherwise crowd out a real tag.
            parts.append("tags: " + ", ".join(self.tags[:6]))
        return " | ".join(p for p in parts if p)


class Bucket(BaseModel):
    """A destination playlist the user wants songs routed into.

    `group` is the axis this playlist sits on. Playlists sharing a group are
    alternatives to one another — "Sunny Drive" and "Late Night Drive" both
    answer *when would you play this*, while "J-Pop" and "Chill Hiphop" answer
    *what does it sound like*. Every song gets at least one playlist from every
    group, so the two questions are answered independently instead of competing.
    Leaving it blank keeps a playlist optional, the way every playlist used to be.
    """

    playlist_id: str
    name: str
    description: str = ""
    group: str = ""

    def as_prompt_row(self) -> str:
        if self.description:
            return f'- "{self.name}": {self.description}'
        return f'- "{self.name}"'


def group_names(buckets: list[Bucket]) -> list[str]:
    """Distinct group names, in the order the buckets were given."""
    seen: list[str] = []
    for b in buckets:
        if b.group and b.group not in seen:
            seen.append(b.group)
    return seen


def missing_groups(playlists: list[str], buckets: list[Bucket]) -> list[str]:
    """Groups this song got nothing from.

    Derived from the assignment rather than stored alongside it, so it stays
    correct for classifications loaded from cache and needs no schema change.
    """
    chosen = {p.strip().lower() for p in playlists}
    out = []
    for name in group_names(buckets):
        members = [b.name.strip().lower() for b in buckets if b.group == name]
        if not chosen.intersection(members):
            out.append(name)
    return out


def bucket_hash(buckets: list[Bucket], prompt_template: str = "") -> str:
    """Cache key for a set of buckets *and* the prompt they were judged under.

    Changing a name, description or group must invalidate cached
    classifications, since they were made against the old definitions. So must
    changing the prompt: results produced under different instructions are not
    interchangeable. Feeding the template in means an edit to the wording
    invalidates the cache automatically, with nothing to remember to bump.
    """
    # Group is only folded in when it is set, so adding the feature does not
    # invalidate the cache of anyone who is not using it.
    payload = sorted(
        [b.name, b.description, b.group] if b.group else [b.name, b.description]
        for b in buckets
    )
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if prompt_template:
        blob += "\x00" + prompt_template
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Assignment(BaseModel):
    video_id: str
    playlists: list[str] = Field(default_factory=list)  # bucket names
    confidence: float = 0.0
    reason: str = ""
    provider: str = ""


class BatchItem(BaseModel):
    """What the model returns per song. Indexes are positions in the batch."""

    idx: int
    playlists: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
