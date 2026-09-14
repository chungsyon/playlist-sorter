"""Provider failover: Gemini free tier -> Claude subscription.

The contract that matters: when a provider runs out of quota, the *same batch*
is retried on the next provider. Nothing is skipped and nothing is classified
twice. When every provider is exhausted the job pauses with its cursor intact
rather than failing, so it can be resumed tomorrow.
"""

from __future__ import annotations

import logging

from .providers import (ClaudeCLIProvider, GeminiProvider, Provider, ProviderError,
                        QuotaExhausted, extract_json_array)

log = logging.getLogger(__name__)

RETRIES_PER_PROVIDER = 2


class AllProvidersExhausted(Exception):
    """Every configured provider is out of quota. Job should pause, not fail."""


class ProviderRouter:
    def __init__(self, providers: list[Provider] | None = None) -> None:
        self.chain: list[Provider] = providers or [GeminiProvider(), ClaudeCLIProvider()]
        self.exhausted: set[str] = set()
        self.tallies: dict[str, int] = {}
        self.events: list[str] = []

    def configured(self) -> list[Provider]:
        return [p for p in self.chain if p.available()]

    def status(self) -> dict:
        return {
            "chain": [
                {
                    "name": p.name,
                    "available": p.available(),
                    "exhausted": p.name in self.exhausted,
                    "count": self.tallies.get(p.name, 0),
                }
                for p in self.chain
            ],
            "events": self.events[-20:],
        }

    def generate_items(self, prompt: str,
                       schema: dict | None = None) -> tuple[list[dict], str]:
        """Return (parsed_items, provider_name), walking the chain on failure.

        Parsing happens in here rather than in the caller, and that placement is
        the point. A provider that answers with unparseable text has not
        answered, so it has to be caught where the retry and the failover live —
        parse outside this loop and one malformed batch kills the whole job
        instead of being retried or handed to the next provider. The Claude CLI
        makes this concrete: it has no structured-output mode, so it is the one
        most likely to return something that will not parse.
        """
        if not self.configured():
            raise AllProvidersExhausted(
                "No providers configured. Set GEMINI_API_KEY in .env, or make "
                "sure the `claude` CLI is on PATH."
            )

        last_error: Exception | None = None

        for provider in self.chain:
            if not provider.available() or provider.name in self.exhausted:
                continue

            for attempt in range(1, RETRIES_PER_PROVIDER + 1):
                try:
                    text = provider.generate_json(prompt, schema)
                    items = extract_json_array(text)
                    self.tallies[provider.name] = self.tallies.get(provider.name, 0) + 1
                    return items, provider.name

                except QuotaExhausted as e:
                    # Out of quota for the rest of this run — stop trying it.
                    self.exhausted.add(provider.name)
                    msg = f"{provider.name} exhausted, switching over. ({e})"
                    log.warning(msg)
                    self.events.append(msg)
                    last_error = e
                    break

                except ProviderError as e:
                    last_error = e
                    if attempt < RETRIES_PER_PROVIDER:
                        log.warning("%s attempt %d failed, retrying: %s",
                                    provider.name, attempt, e)
                        continue
                    msg = f"{provider.name} failed after {attempt} attempts: {e}"
                    log.warning(msg)
                    self.events.append(msg)

        raise AllProvidersExhausted(
            f"All providers exhausted or failing. Last error: {last_error}"
        )
