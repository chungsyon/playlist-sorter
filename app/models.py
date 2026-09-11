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
            parts.append("tags: " + ", ".join(self.tags[:5]))
        return " | ".join(p for p in parts if p)


class Bucket(BaseModel):
    """A destination playlist the user wants songs routed into."""

    playlist_id: str
    name: str
    description: str = ""

    def as_prompt_row(self) -> str:
        if self.description:
            return f'- "{self.name}": {self.description}'
        return f'- "{self.name}"'


def bucket_hash(buckets: list[Bucket], prompt_template: str = "") -> str:
    """Cache key for a set of buckets *and* the prompt they were judged under.

    Changing a name or description must invalidate cached classifications,
    since they were made against the old definitions. So must changing the
    prompt: results produced under different instructions are not
    interchangeable. Feeding the template in means an edit to the wording
    invalidates the cache automatically, with nothing to remember to bump.
    """
    payload = sorted((b.name, b.description) for b in buckets)
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
