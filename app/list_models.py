"""Show which Gemini models your key can reach.

    uv run python -m app.list_models

Useful because model IDs move fast — if GEMINI_MODEL in .env is wrong, this
tells you what to put there instead.
"""

from __future__ import annotations

import sys

from .config import settings
from .providers import GeminiProvider, ProviderError


def main() -> int:
    if not settings.has_gemini:
        print("GEMINI_API_KEY is not set in .env")
        print("Get a free key at https://aistudio.google.com/apikey")
        return 1

    try:
        models = GeminiProvider().list_models()
    except ProviderError as e:
        print(f"Failed: {e}")
        return 1

    flash = [m for m in models if "flash" in m.lower()]

    print(f"Currently configured: {settings.gemini_model}\n")
    print("Flash / Flash-Lite models available to your key:")
    for name in sorted(flash):
        print(f"  {name.removeprefix('models/')}")

    if not flash:
        print("  (none found — showing everything instead)")
        for name in sorted(models):
            print(f"  {name.removeprefix('models/')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
