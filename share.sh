#!/usr/bin/env bash
#
# Build a copy of Playlist Sorter to hand to someone else.
#
#   ./share.sh                 -> ~/Desktop/PlaylistSorter.zip
#   ./share.sh /path/to/out    -> /path/to/out.zip
#
# Your OAuth client is copied in, because it is the app's identity and is
# meant to be shared. Your YouTube session, API keys, sorted-song database
# and backups are not.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
ok()  { printf '%s  ok%s  %s\n' "$GREEN" "$RESET" "$*"; }
die() { printf '%s FAIL%s  %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

OUT="${1:-$HOME/Desktop/PlaylistSorter}"
rm -rf "$OUT" "$OUT.zip"
mkdir -p "$OUT"

# .gitignore is already the authoritative list of what must never leave this
# machine, so reuse it rather than maintaining a second exclusion list here.
# --others picks up the launcher even before it is committed.
printf '%s==>%s Copying files\n' "$BOLD" "$RESET"
git ls-files -z --cached --others --exclude-standard \
  | tar --null -cf - -T - \
  | tar -xf - -C "$OUT"
ok "$(find "$OUT" -type f | wc -l | tr -d ' ') files"

# Development leftovers the recipient has no use for, in a folder whose whole
# point is that one file is obviously the one to open.
rm -f "$OUT/share.sh" "$OUT/PLAN.md" "$OUT/StartingPromptPlaySort.md"

# A fresh .env from the template, with only the OAuth client carried over.
printf '%s==>%s Preparing their .env\n' "$BOLD" "$RESET"
[ -f .env ] || die "No .env here — run the app once and fill in Setup first."
CLIENT_ID="$(sed -n 's/^YTM_OAUTH_CLIENT_ID=//p' .env | tail -1)"
CLIENT_SECRET="$(sed -n 's/^YTM_OAUTH_CLIENT_SECRET=//p' .env | tail -1)"
[ -n "$CLIENT_ID" ] || die "YTM_OAUTH_CLIENT_ID is blank in your .env — nothing to share."

sed -e "s|^YTM_OAUTH_CLIENT_ID=.*|YTM_OAUTH_CLIENT_ID=$CLIENT_ID|" \
    -e "s|^YTM_OAUTH_CLIENT_SECRET=.*|YTM_OAUTH_CLIENT_SECRET=$CLIENT_SECRET|" \
    .env.example > "$OUT/.env"
ok "OAuth client copied, Gemini key left blank"

# Safety net. A mistake here leaks a live YouTube session, so check the built
# folder itself rather than trusting the copy step.
printf '%s==>%s Checking for leaked secrets\n' "$BOLD" "$RESET"
for f in browser.json oauth.json; do
  [ -e "$OUT/$f" ] && die "$f ended up in the package. Stopping."
done
[ -e "$OUT/data" ] && die "data/ ended up in the package. Stopping."
MY_KEY="$(sed -n 's/^GEMINI_API_KEY=//p' .env | tail -1)"
if [ -n "$MY_KEY" ] && grep -rqF -- "$MY_KEY" "$OUT"; then
  die "Your Gemini key ended up in the package. Stopping."
fi
MY_LASTFM="$(sed -n 's/^LASTFM_API_KEY=//p' .env | tail -1)"
if [ -n "$MY_LASTFM" ] && grep -rqF -- "$MY_LASTFM" "$OUT"; then
  die "Your Last.fm key ended up in the package. Stopping."
fi
ok "no session files, no API keys"

cat > "$OUT/START HERE.txt" <<'NOTE'
PLAYLIST SORTER
===============

Sorts a YouTube Music playlist into your other playlists. You review every
song before anything is changed.

Works on Mac only.


STEP 1 — Get a free Gemini key (2 minutes)
------------------------------------------
  1. Go to        https://aistudio.google.com/apikey
  2. Click        "Create API key"
  3. Choose       "Create API key in new project"
  4. Copy the key that appears. Keep the tab open.

Do NOT add a billing account to that project. With no payment method on
file there is nothing that can be charged — that is what keeps this free.


STEP 2 — Start the app
----------------------
  Double-click    Start Playlist Sorter.command

The first start installs everything and takes a few minutes. Later starts
take seconds. Your browser opens on its own when it's ready.

If macOS says the file "cannot be opened" or is from an "unidentified
developer":

  - Right-click the file and choose Open, then click Open again, OR
  - Open System Settings > Privacy & Security, scroll down, and click
    "Open Anyway" next to the message about this file.

You only have to do that once.

KEEP THE BLACK TERMINAL WINDOW OPEN while you use the app. Closing it
stops the app. Everything you've done is saved, so you can start it again.


STEP 3 — In the browser
-----------------------
  Setup        Paste your Gemini key from Step 1. Press Save.
  Connect      Sign in with Google. You get a short code to type in on a
               Google page, and the app picks it up by itself.
  Configure    Pick the playlist to sort, tick where songs can go, and
               write one line describing each destination. Do write these
               — "Slow Down" means nothing to the app on its own, but
               "mellow, low-tempo, for winding down" does. This matters
               more than any other setting.
  Sort         Watch it work. You can pause; nothing is lost.
  Review       Check every song. Click a label to move a song somewhere
               else.

Nothing is written to YouTube Music until you press Apply on the Review
screen. Before you press it, nothing has changed.


IF SOMETHING GOES WRONG
-----------------------
Send whoever gave you this a photo of the black window. The error is in
there and it means something to them.
NOTE
ok "START HERE.txt written"

printf '%s==>%s Zipping\n' "$BOLD" "$RESET"
( cd "$(dirname "$OUT")" && zip -qr "$(basename "$OUT").zip" "$(basename "$OUT")" )
ok "$OUT.zip  ($(du -h "$OUT.zip" | cut -f1 | tr -d ' '))"

cat <<EOF

${BOLD}Ready to send:${RESET} $OUT.zip

${DIM}Before they can sign in, add their Google account as a test user:
  console.cloud.google.com > APIs & Services > OAuth consent screen > Test users${RESET}

EOF
