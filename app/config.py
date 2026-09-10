"""Configuration loaded from .env.

Everything has a working default except the API keys. Values can also be set
from the app's Setup screen at runtime, which writes them back to .env — a
stranger installing this should never have to open a text editor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

# First run: give the user a real .env seeded from the documented template.
if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
    ENV_PATH.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

load_dotenv(ENV_PATH)


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key) or default)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _read_env() -> dict[str, Any]:
    """Every setting, read fresh from the environment.

    Kept separate from the dataclass defaults so reload_settings() and the
    initial construction share one definition instead of drifting apart.
    """
    return {
        "gemini_api_key": _str("GEMINI_API_KEY"),
        "gemini_model": _str("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        "gemini_rpm": _int("GEMINI_RPM", 10),
        "claude_enabled": _bool("CLAUDE_ENABLED", True),
        "claude_model": _str("CLAUDE_MODEL", "sonnet"),
        "claude_cli_path": _str("CLAUDE_CLI_PATH", "claude"),
        "lastfm_api_key": _str("LASTFM_API_KEY"),
        "ytm_auth_file": ROOT / _str("YTM_AUTH_FILE", "browser.json"),
        "ytm_oauth_file": ROOT / _str("YTM_OAUTH_FILE", "oauth.json"),
        "ytm_oauth_client_id": _str("YTM_OAUTH_CLIENT_ID"),
        "ytm_oauth_client_secret": _str("YTM_OAUTH_CLIENT_SECRET"),
        "batch_size": _int("BATCH_SIZE", 50),
        "ytm_write_chunk_size": _int("YTM_WRITE_CHUNK_SIZE", 50),
        "ytm_write_delay_seconds": _float("YTM_WRITE_DELAY_SECONDS", 1.5),
        "confidence_threshold": _float("CONFIDENCE_THRESHOLD", 0.55),
        "port": _int("PORT", 8765),
        "database_path": ROOT / _str("DATABASE_PATH", "data/sorter.db"),
        "backup_dir": ROOT / _str("BACKUP_DIR", "backups"),
    }


@dataclass(frozen=True)
class Settings:
    # Defaults here are placeholders; real values arrive from _read_env().
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_rpm: int = 10

    claude_enabled: bool = True
    claude_model: str = "sonnet"
    claude_cli_path: str = "claude"

    lastfm_api_key: str = ""

    ytm_auth_file: Path = ROOT / "browser.json"
    ytm_oauth_file: Path = ROOT / "oauth.json"
    ytm_oauth_client_id: str = ""
    ytm_oauth_client_secret: str = ""

    batch_size: int = 50
    ytm_write_chunk_size: int = 50
    ytm_write_delay_seconds: float = 1.5
    confidence_threshold: float = 0.55

    port: int = 8765
    database_path: Path = ROOT / "data/sorter.db"
    backup_dir: Path = ROOT / "backups"

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_lastfm(self) -> bool:
        return bool(self.lastfm_api_key)

    @property
    def has_oauth_client(self) -> bool:
        return bool(self.ytm_oauth_client_id and self.ytm_oauth_client_secret)

    @property
    def ytm_authenticated(self) -> bool:
        return self.ytm_oauth_file.exists() or self.ytm_auth_file.exists()


settings = Settings(**_read_env())


def _ensure_dirs() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)


_ensure_dirs()


# ---------------------------------------------------------------------------
# runtime updates
# ---------------------------------------------------------------------------

# Written by the Setup screen. Everything else stays a file-only knob.
EDITABLE_KEYS = {
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_RPM",
    "LASTFM_API_KEY",
    "CLAUDE_ENABLED",
    "CLAUDE_MODEL",
    "YTM_OAUTH_CLIENT_ID",
    "YTM_OAUTH_CLIENT_SECRET",
    "BATCH_SIZE",
    "CONFIDENCE_THRESHOLD",
}


def update_env(updates: dict[str, str]) -> None:
    """Upsert keys into .env, then make them live.

    Rewrites in place so the file keeps its comments — those comments are the
    documentation for anyone who does open it.
    """
    unknown = set(updates) - EDITABLE_KEYS
    if unknown:
        raise ValueError(f"Not editable from the app: {', '.join(sorted(unknown))}")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    if remaining:
        lines.append("")
        lines.append("# Added from the Setup screen")
        lines.extend(f"{k}={v}" for k, v in remaining.items())

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for key, value in updates.items():
        os.environ[key] = value

    reload_settings()


def reload_settings() -> None:
    """Refresh the shared Settings instance in place.

    Every module does `from .config import settings`, so those references must
    keep pointing at the same object — hence mutating a frozen dataclass rather
    than rebinding the name.
    """
    load_dotenv(ENV_PATH, override=True)
    values = _read_env()
    names = {f.name for f in fields(Settings)}
    for key, value in values.items():
        if key in names:
            object.__setattr__(settings, key, value)
    _ensure_dirs()
