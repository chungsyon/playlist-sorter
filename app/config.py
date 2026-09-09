"""Configuration loaded from .env. Every value has a default except the API keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


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


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = _str("GEMINI_API_KEY")
    gemini_model: str = _str("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_rpm: int = _int("GEMINI_RPM", 10)

    claude_enabled: bool = _bool("CLAUDE_ENABLED", True)
    claude_model: str = _str("CLAUDE_MODEL", "sonnet")
    claude_cli_path: str = _str("CLAUDE_CLI_PATH", "claude")

    lastfm_api_key: str = _str("LASTFM_API_KEY")

    ytm_auth_file: Path = ROOT / (_str("YTM_AUTH_FILE", "browser.json"))

    batch_size: int = _int("BATCH_SIZE", 50)
    ytm_write_chunk_size: int = _int("YTM_WRITE_CHUNK_SIZE", 50)
    ytm_write_delay_seconds: float = _float("YTM_WRITE_DELAY_SECONDS", 1.5)
    confidence_threshold: float = _float("CONFIDENCE_THRESHOLD", 0.55)

    port: int = _int("PORT", 8765)
    database_path: Path = ROOT / (_str("DATABASE_PATH", "data/sorter.db"))
    backup_dir: Path = ROOT / (_str("BACKUP_DIR", "backups"))

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_lastfm(self) -> bool:
        return bool(self.lastfm_api_key)

    @property
    def ytm_authenticated(self) -> bool:
        return self.ytm_auth_file.exists()


settings = Settings()

settings.database_path.parent.mkdir(parents=True, exist_ok=True)
settings.backup_dir.mkdir(parents=True, exist_ok=True)
