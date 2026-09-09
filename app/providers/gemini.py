"""Gemini free-tier provider.

Important: this uses an AI Studio API key with NO billing account attached,
which is a different thing from a Google AI Pro subscription. The subscription
only covers the AI Studio website; API keys are billed separately. A key with no
billing account attached cannot be charged, which is what keeps this free.
"""

from __future__ import annotations

import logging
import threading
import time

from ..config import settings
from .base import RESPONSE_SCHEMA, Provider, ProviderError, QuotaExhausted

log = logging.getLogger(__name__)

# Substrings that mean "out of quota" rather than "broken".
_QUOTA_MARKERS = (
    "resource_exhausted", "quota", "rate limit", "ratelimit",
    "429", "exceeded your current quota",
)


class _RateLimiter:
    """Self-throttle to stay under the free tier's per-minute cap."""

    def __init__(self, rpm: int) -> None:
        self.min_interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            sleep_for = self.min_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self) -> None:
        self._client = None
        self._limiter = _RateLimiter(settings.gemini_rpm)
        self.model = settings.gemini_model

    def available(self) -> bool:
        return settings.has_gemini

    def _get_client(self):
        if self._client is None:
            from google import genai  # imported lazily so the app boots without the key
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    def generate_json(self, prompt: str) -> str:
        if not self.available():
            raise ProviderError("GEMINI_API_KEY is not set in .env")

        from google.genai import types

        self._limiter.wait()

        try:
            resp = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.2,  # classification wants consistency, not flair
                    # We pass no tools; disabling AFC silences a spurious SDK warning.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(m in msg for m in _QUOTA_MARKERS):
                raise QuotaExhausted(f"Gemini quota exhausted: {e}") from e
            raise ProviderError(f"Gemini call failed: {e}") from e

        text = getattr(resp, "text", None)
        if not text:
            raise ProviderError("Gemini returned an empty response.")
        return text

    def list_models(self) -> list[str]:
        """Diagnostic helper — what this key can actually reach."""
        try:
            return [m.name for m in self._get_client().models.list()]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Could not list models: {e}") from e
