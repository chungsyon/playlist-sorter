# Playlist Sorter — Vibe-Based YouTube Music Playlist Router

## Context

You have a large YouTube Music playlist and a set of hand-made destination playlists
("Party Vibes", "Work Out", "Slow Down", "Chill Hiphop", "Late Night Drive", "Hype Up", …).
You want an app that reads the source playlist, decides which vibe bucket(s) each song
belongs to, shows you the proposal, and then writes the assignments into YouTube Music.

**Hard constraint: zero additional spend.** That constraint drove the research below and is
what shaped the architecture. Three findings changed the original premise:

1. **Your Gemini subscription does not grant API access.** Google's official docs are explicit:
   *"Google AI plan benefits for developer usage apply only within the Google AI Studio web
   interface. Direct use of the Gemini API (such as using API keys or external applications) is
   billed and managed separately."* So the plan cannot "use the Gemini plan I already have."
   **But there is a genuinely free path:** the Gemini API **free tier** — an AI Studio API key with
   no billing account attached. Input and output tokens on Flash / Flash-Lite are $0, no card
   required. It is rate-limited (~10–15 RPM, ~250–1,500 requests/day depending on model), which
   the batching design below makes a non-issue. Tradeoff: free-tier prompts are used to improve
   Google's products. We only ever send song titles/artists, so this is low-sensitivity — but
   it's your call to make knowingly.

2. **Your Claude subscription genuinely does cover this as a fallback.** Pro/Max plans cover
   `claude -p` (non-interactive Claude Code) and Agent SDK usage against your normal subscription
   limits. So the failover you described works at no extra cost — with one architectural
   consequence: it only works on a machine where the `claude` CLI is logged in. **This is the main
   reason the app must run locally**, which you also chose.

3. **There is no free write path through the official YouTube Data API.** `playlistItems.insert`
   costs **50 quota units** against a **10,000 unit/day** free allocation = **200 song additions
   per day, hard stop**. With multi-bucket assignment, a 2,000-song playlist would take ~two weeks.
   The official API is therefore unusable for the write side, and the design uses the
   `ytmusicapi` library (browser-session auth, no quota) instead. Risks are stated in full below.

Also note: YouTube Music killed nothing here, but **Spotify-style audio features do not exist for
us** — there is no energy/danceability/valence signal available. Vibe classification is derived
from metadata + free community tags + the model's own knowledge of the songs. This is why the
review screen matters.

---

## Architecture

A local Python app: FastAPI backend + a plain HTML/JS frontend served at `http://localhost:8765`.
No build step, no hosting, no cloud, no secrets leaving the machine.

```
Browser UI (localhost:8765)
        │
   FastAPI backend
        ├── ytm.py         → ytmusicapi: read source, list destinations, write additions
        ├── enrich.py      → Last.fm top-tags per track (free, cached)
        ├── classify.py    → batches songs, builds prompt, validates JSON response
        ├── router.py      → Gemini free tier → (on quota exhaustion) → Claude CLI
        └── db.py          → SQLite: track cache, tag cache, classification cache, job state
```

**Everything is cached and every job is resumable.** This is what makes "unlimited songs" real:
a run that dies at song 3,400 resumes at 3,400, and re-running a playlist you've already sorted
costs zero model calls.

### The key move: batching

Naive per-song calls would blow through any free tier. Instead, songs are sent in **batches of
~50**, each row compacted to `idx | title | artist | album | year | tags`. The model returns a
JSON array of `{idx, playlists[], confidence, reason}`.

| Playlist size | LLM requests | Time at 10 RPM |
|---|---|---|
| 2,000 songs | ~40 | ~4 min |
| 10,000 songs | ~200 | ~20 min |

Even the most conservative free-tier quota (250 req/day) comfortably covers a 10,000-song
playlist. The failover to Claude becomes a rare safety net, not the normal path.

---

## Components to build

All new files under the project root. Use `uv` to pin **Python 3.12** — system Python is 3.9.6 and
current `ytmusicapi` requires 3.10+.

### `app/ytm.py` — YouTube Music access
- Auth via `ytmusicapi.setup(filepath="browser.json", headers_raw=...)`; the UI has a paste-box for
  browser request headers, with a link to the setup instructions. Credentials last ~2 years.
- `get_library_playlists()` → populate both dropdowns (source + destination multi-select).
- `get_playlist(playlistId, limit=None)` → **`limit=None` retrieves the entire playlist**, fully
  paginated. This is the "no restrictions" requirement. Per-track fields we use: `videoId`,
  `title`, `artists`, `album`, `duration_seconds`, `isAvailable`.
- Write: `add_playlist_items(playlistId, videoIds=[...], duplicates=False)` in chunks of ~50,
  with a deliberate 1–2s sleep between calls. **Never** call `remove_playlist_items` — the source
  playlist is never modified (your "multiple playlists allowed / add-only" choice).
- Before writing, fetch each destination's existing `videoId` set and diff, so re-runs don't
  attempt duplicate adds.

### `app/enrich.py` — free vibe signal
- Last.fm `track.getTopTags` (free API key, non-commercial, ~2 req/sec) → top 5 tags per track.
  These are real listener tags like *chill*, *workout*, *summer*, *melancholy* and measurably
  improve classification of songs the model doesn't know well.
- Throttled, cached permanently in SQLite, and **optional** — if no Last.fm key is configured the
  pipeline runs on metadata alone.

### `app/providers/` — the model layer
- `base.py` — `Provider.classify(batch, buckets) -> list[Assignment]`, plus a `QuotaExhausted`
  exception type.
- `gemini.py` — `google-genai` SDK, free-tier API key, `gemini-flash-lite` (highest free RPD) with
  Flash as an upgrade toggle. Uses **structured output** (`response_schema`) so responses are
  schema-valid JSON, not parsed prose. Raises `QuotaExhausted` on HTTP 429 / `RESOURCE_EXHAUSTED`.
- `claude_cli.py` — subprocesses `claude -p <prompt> --output-format json --model sonnet`, parses
  the JSON envelope's `result` field. Verified available on this machine. Bills to your existing
  Claude subscription.

### `app/router.py` — failover
Ordered chain `[gemini, claude_cli]`. On `QuotaExhausted`, log the switch, mark the provider dead
for the rest of the run, and retry the *same batch* on the next provider — no songs are skipped or
double-counted. If every provider is exhausted, persist job state and surface
"paused — quota exhausted, resume later" in the UI. Each classification row records which provider
produced it, so the UI can show "1,850 by Gemini / 150 by Claude".

### `app/classify.py` — prompt + batching
- Prompt states the user's bucket names **and their descriptions** (the UI lets you write a
  one-line description per destination playlist — this is the single biggest accuracy lever).
- Instructs: assign 0–N buckets per song, emit `confidence` 0–1, and a short `reason`.
- Explicitly permits an empty assignment rather than forcing a bad fit.
- Validates indices round-trip and re-issues any batch that comes back malformed.

### `app/db.py` — SQLite (`sorter.db`)
`tracks`, `tags`, `classifications` (keyed by `videoId` + hash of the bucket set, so changing your
buckets correctly invalidates), `jobs` (status, cursor, provider tallies).

### `web/` — UI
Four screens: **Connect** → **Configure** (pick source, pick destinations, add descriptions) →
**Progress** (live counter, current provider, pause/resume) → **Review**.

The Review screen is the deliverable: songs grouped by destination playlist, each row showing
title/artist, confidence, the model's reason, and checkboxes to add/remove buckets. Low-confidence
rows are visually flagged and sorted to the top. A "songs matched to nothing" section catches the
leftovers. **Nothing is written to YouTube Music until you press Apply.** A JSON snapshot of all
affected playlists is written to `backups/` immediately before any write.

---

## Build order

1. `db.py` + `ytm.py` + auth paste-box — prove we can read the full playlist and list destinations.
2. `classify.py` + `gemini.py` — classify a 50-song slice end-to-end, inspect quality by hand.
3. `enrich.py` — add Last.fm tags, re-run the same slice, compare quality against step 2.
4. `claude_cli.py` + `router.py` — force failover by faking a 429 and confirm no batch is lost.
5. Job runner: batching, resume, pause.
6. Review UI + Apply, with backups and duplicate-diffing.

Steps 1–2 are the real risk. If ytmusicapi auth or the classification quality disappoints, we find
out on day one rather than after building the UI.

---

## Verification

- **Read path:** point at the real source playlist, confirm the fetched count matches what the
  YouTube Music app shows — proves `limit=None` pagination works at full size.
- **Classification quality:** hand-pick 30 songs whose bucket you already know; measure agreement.
  Iterate on bucket *descriptions* before touching prompt wording.
- **Failover:** monkeypatch the Gemini provider to raise `QuotaExhausted` on batch 3; assert the
  final result set has every song classified exactly once and the tally shows both providers.
- **Resume:** kill the process mid-job, restart, confirm it resumes at the cursor and issues no
  duplicate model calls.
- **Write path (do this first):** create a throwaway destination playlist, apply 5 songs, verify in
  the YouTube Music app, re-apply the same 5 and confirm the duplicate-diff results in zero writes.
- **Cost:** confirm the Google Cloud project has **no billing account attached** — that is the
  structural guarantee you cannot be charged, rather than a promise to stay under a limit.

---

## Risks — stated plainly

- **`ytmusicapi` is unofficial — accepted deliberately.** You chose the ytmusicapi-only path over
  the official API's 200-additions/day cap. It reverse-engineers YouTube Music's internal
  endpoints: widely used and actively maintained, but outside YouTube's Terms of Service, liable to
  break without warning when Google changes internals, and carrying some non-zero account risk.
  Mitigations built into the design: writes are chunked to ~50 and paced 1–2s apart rather than
  fired as fast as possible, destination playlists are diffed first so no redundant calls are made,
  and all reads are done once and cached so a re-run touches YouTube Music only for actual writes.
- **Free-tier Gemini prompts are used to improve Google's products.** We send only song
  titles/artists/tags.
- **Claude fallback consumes your Claude Code subscription limits** — heavy sorting could eat into
  the quota you use for other work.
- **No audio analysis exists.** Classification rests on metadata, community tags, and model
  knowledge. Well-known songs will be accurate; obscure tracks will be weaker. The confidence
  score plus the review screen are the mitigation, not a fix.
- **YouTube Music caps individual playlists around 5,000 tracks** (10,000 for Liked Music). Our app
  imposes no limit; the platform does.

## Sources

- [Google AI Plans — API access is separate](https://ai.google.dev/gemini-api/docs/google-ai-plans)
- [Gemini API pricing — free tier](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [YouTube Data API quota costs](https://developers.google.com/youtube/v3/determine_quota_cost)
- [ytmusicapi browser authentication](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)
- [ytmusicapi playlist reference](https://ytmusicapi.readthedocs.io/en/stable/reference/playlists.html)
- [Spotify Web API deprecations, Nov 2024](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)
