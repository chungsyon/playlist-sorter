"""Provider interface shared by Gemini and the Claude CLI."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod


class QuotaExhausted(Exception):
    """Provider is out of quota for now. The router moves to the next one and
    retries the *same* batch, so no songs are lost."""


class ProviderError(Exception):
    """Something else went wrong — malformed output, crash, timeout."""


# Shape both providers must return: one entry per song in the batch.
RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "idx": {"type": "integer"},
            "playlists": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["idx", "playlists", "confidence"],
    },
}


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """False if unconfigured — the router skips it silently."""

    @abstractmethod
    def generate_json(self, prompt: str) -> str:
        """Return raw JSON text. Raise QuotaExhausted or ProviderError."""


def extract_json_array(raw: str) -> list[dict]:
    """Pull a JSON array out of a model response.

    Gemini's structured output returns clean JSON, but the Claude fallback
    sometimes wraps it in prose or a fenced block, so we degrade gracefully
    rather than failing the batch.
    """
    if not raw or not raw.strip():
        raise ProviderError("Empty response.")

    text = raw.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ProviderError(f"No JSON array found in: {raw[:200]}") from None
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ProviderError(f"Malformed JSON: {e}") from e

    if isinstance(parsed, dict):
        for key in ("results", "assignments", "songs", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break

    if not isinstance(parsed, list):
        raise ProviderError(f"Expected a JSON array, got {type(parsed).__name__}.")

    return [item for item in parsed if isinstance(item, dict)]
