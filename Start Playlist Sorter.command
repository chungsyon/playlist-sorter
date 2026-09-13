#!/usr/bin/env bash
#
# Playlist Sorter — double-click this file to start.
#
# Installs everything on first run, then opens the app in your browser.
# Nothing to type.

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'

# Wait for a keypress before exiting, so a double-clicked window doesn't
# vanish with the error still on screen.
die() { printf '\n%s\n\nPress return to close this window.\n' "$*"; read -r _; exit 1; }

# uv installs itself into one of these. A double-clicked shell hasn't
# necessarily picked them up on PATH yet.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Honour a changed PORT in .env rather than assuming the default.
PORT="$(sed -n 's/^PORT=//p' .env 2>/dev/null | tail -1 | tr -d ' ')"
PORT="${PORT:-8765}"
URL="http://localhost:$PORT"

# Already running? Just show it, instead of failing to bind the port.
if curl -sf -o /dev/null "$URL" 2>/dev/null; then
  printf '%sPlaylist Sorter is already running.%s Opening %s\n' "$BOLD" "$RESET" "$URL"
  open "$URL"
  exit 0
fi

# First run: install. setup.sh is safe to re-run, but skipping it when the
# environment already exists keeps normal startup instant.
if [ ! -d .venv ]; then
  printf '%sFirst run — installing. This takes a few minutes.%s\n' "$BOLD" "$RESET"
  printf '%sYou only have to wait for this once.%s\n\n' "$DIM" "$RESET"
  ./setup.sh || die "Install failed. Send the messages above to whoever gave you this."
fi

# Open the browser once the server actually answers — not before, or the
# browser shows a connection error and the user thinks it's broken.
(
  for _ in $(seq 1 240); do
    curl -sf -o /dev/null "$URL" 2>/dev/null && { open "$URL"; exit 0; }
    sleep 0.5
  done
) &

printf '\n%sStarting Playlist Sorter…%s\n' "$BOLD" "$RESET"
printf '%sYour browser will open at %s in a moment.%s\n\n' "$DIM" "$URL" "$RESET"
printf '%s>>> KEEP THIS WINDOW OPEN while you use the app. <<<%s\n' "$BOLD" "$RESET"
printf '%sClosing it stops the app. To stop on purpose: press Control-C.%s\n\n' "$DIM" "$RESET"

uv run python -m app.main || die "Playlist Sorter stopped unexpectedly."
