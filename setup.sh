#!/usr/bin/env bash
#
# Playlist Sorter — one-command setup.
#
#   ./setup.sh
#
# Installs uv if needed, creates the virtualenv, installs dependencies, and
# seeds .env. Safe to re-run.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$BOLD" "$RESET" "$*" "$RESET"; }
ok()   { printf '%s  ok%s  %s\n' "$GREEN" "$RESET" "$*"; }
die()  { printf '%s FAIL%s  %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say "${BOLD}Playlist Sorter — setup${RESET}"
say "${DIM}$ROOT${RESET}"

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
step "Checking for uv"
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  say "uv not found. Installing from astral.sh…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "Could not install uv. See https://docs.astral.sh/uv/"
  # The installer drops uv in one of these; pick it up without a new shell.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && export PATH="$candidate:$PATH"
  done
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH. Open a new terminal and re-run ./setup.sh"
  ok "uv installed"
fi

# ---------------------------------------------------------------------------
# 2. Virtualenv location
#
# A venv inside iCloud Drive (or Dropbox/OneDrive) gets its files evicted to
# the cloud when they go cold. Every import then blocks on a download and the
# app appears to hang at 0% CPU with no output. Keeping the venv outside the
# synced folder avoids that entirely.
# ---------------------------------------------------------------------------
step "Choosing a virtualenv location"
case "$ROOT" in
  *"Mobile Documents"*|*"iCloud"*|*"Dropbox"*|*"OneDrive"*|*"Google Drive"*)
    VENV="$HOME/.venvs/playlist-sorter"
    mkdir -p "$HOME/.venvs"
    say "${DIM}This folder is inside a syncing service; the venv goes outside it.${RESET}"
    ;;
  *)
    VENV="$ROOT/.venv"
    ;;
esac
export UV_PROJECT_ENVIRONMENT="$VENV"
ok "$VENV"

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
step "Installing dependencies"
uv sync || die "uv sync failed."
ok "dependencies installed"

# A .venv symlink means plain `uv run …` finds the environment afterwards,
# with no environment variable to remember.
if [ "$VENV" != "$ROOT/.venv" ]; then
  ln -sfn "$VENV" "$ROOT/.venv"
  ok ".venv -> $VENV"
fi

# ---------------------------------------------------------------------------
# 4. Config
# ---------------------------------------------------------------------------
step "Preparing .env"
if [ -f .env ]; then
  ok ".env already exists — left untouched"
else
  cp .env.example .env
  ok ".env created from .env.example"
fi

# ---------------------------------------------------------------------------
# 5. Done
# ---------------------------------------------------------------------------
step "Setup complete"
cat <<EOF

Start the app:

    uv run python -m app.main

Then open http://localhost:8765 and fill in your keys on the Setup screen.
You will need:

  • A Gemini API key      https://aistudio.google.com/apikey
                          (create it in a NEW project, no billing account)

  • A Google OAuth client https://console.cloud.google.com/
                          enable "YouTube Data API v3", then create an OAuth
                          client of type "TV and Limited Input devices"

  • A Last.fm key         optional, improves accuracy
                          https://www.last.fm/api/account/create

Check everything is wired up at any time with:

    uv run python -m app.doctor

EOF
