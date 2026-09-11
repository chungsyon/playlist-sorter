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


def grouped_response_schema(groups: list[str],
                            optional_key: str | None = None) -> dict:
    """One answer slot per group, instead of a single flat list.

    Asking for the picks grouped is what actually holds the "at least one from
    every group" rule. A flat list lets the model answer one group twice and
    forget another — telling it not to only gets so far, but a required key per
    group makes the mistake impossible to write down.
    """
    # minItems forbids the other way out: a required key holding an empty list.
    slots = {
        g: {"type": "array", "items": {"type": "string"}, "minItems": 1}
        for g in groups
    }
    if optional_key:
        # Present but not required — ungrouped playlists are genuinely optional.
        slots[optional_key] = {"type": "array", "items": {"type": "string"}}

    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "idx": {"type": "integer"},
                "picks": {
                    "type": "object",
                    "properties": slots,
                    "required": list(groups),
                },
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["idx", "picks", "confidence"],
        },
    }


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """False if unconfigured — the router skips it silently."""

    @abstractmethod
    def generate_json(self, prompt: str, schema: dict | None = None) -> str:
        """Return raw JSON text. Raise QuotaExhausted or ProviderError.

        `schema` overrides RESPONSE_SCHEMA for providers that can enforce one.
        Providers that cannot are free to ignore it — the prompt describes the
        same shape in words.
        """


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
