"""Claude fallback, via the `claude` CLI already logged in on this Mac.

Anthropic's Pro/Max plans cover `claude -p` (non-interactive Claude Code) against
your normal subscription limits, so this costs nothing extra. The consequence is
that it only works locally, on a machine where the CLI is authenticated — which
is why this app runs on localhost rather than being deployed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from ..config import settings
from .base import Provider, ProviderError, QuotaExhausted

log = logging.getLogger(__name__)

_LIMIT_MARKERS = (
    "usage limit", "rate limit", "quota", "429",
    "too many requests", "limit reached", "upgrade to",
)

TIMEOUT_SECONDS = 300


class ClaudeCLIProvider(Provider):
    name = "claude"

    def __init__(self) -> None:
        self.cli = settings.claude_cli_path
        self.model = settings.claude_model

    def available(self) -> bool:
        return settings.claude_enabled and shutil.which(self.cli) is not None

    def generate_json(self, prompt: str, schema: dict | None = None) -> str:
        # No structured-output mode on the CLI; the prompt describes the shape.
        del schema
        if not self.available():
            raise ProviderError(
                f"`{self.cli}` not found on PATH, or CLAUDE_ENABLED is false."
            )

        cmd = [
            self.cli, "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"Claude CLI timed out after {TIMEOUT_SECONDS}s") from e
        except OSError as e:
            raise ProviderError(f"Could not run Claude CLI: {e}") from e

        combined = f"{proc.stdout}\n{proc.stderr}".lower()

        if proc.returncode != 0:
            if any(m in combined for m in _LIMIT_MARKERS):
                raise QuotaExhausted(f"Claude subscription limit hit: {proc.stderr[:300]}")
            raise ProviderError(
                f"Claude CLI exited {proc.returncode}: {proc.stderr[:300]}"
            )

        return self._unwrap(proc.stdout, combined)

    @staticmethod
    def _unwrap(stdout: str, combined: str) -> str:
        """`--output-format json` returns an envelope; the model's text is in
        its `result` field."""
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            # Some versions print plain text despite the flag; use it as-is.
            if not stdout.strip():
                raise ProviderError("Claude CLI returned nothing.") from None
            return stdout

        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                detail = str(envelope.get("result", ""))[:300]
                if any(m in combined for m in _LIMIT_MARKERS):
                    raise QuotaExhausted(f"Claude subscription limit hit: {detail}")
                raise ProviderError(f"Claude returned an error: {detail}")

            result = envelope.get("result")
            if isinstance(result, str) and result.strip():
                return result

        return stdout
