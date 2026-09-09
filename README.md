# Playlist Sorter

Sorts a YouTube Music playlist of any size into vibe-based destination playlists,
using Gemini's free tier and falling back to your Claude subscription when it runs
dry. Runs entirely on your Mac. Costs nothing beyond what you already pay.

See [PLAN.md](PLAN.md) for the architecture and the research behind it.

---

## Setup

### 1. Install

```bash
uv sync
```

### 2. Fill in `.env`

Two things need filling in; the rest have working defaults.

**`GEMINI_API_KEY`** — go to <https://aistudio.google.com/apikey>, create a key in a
**new project**, and **do not attach a billing account**. No billing account means
you cannot be charged, which is the actual guarantee rather than a promise to stay
under a limit.

> Your Google AI Pro subscription does *not* cover API keys — it only covers the
> AI Studio website. The free-tier key is what keeps this project free.

**`LASTFM_API_KEY`** *(optional but recommended)* — <https://www.last.fm/api/account/create>.
Free, instant. Pulls community tags like "chill" / "workout" that noticeably improve
accuracy on songs the model doesn't recognise. Leave blank and everything still works.

Nothing to configure for Claude — it shells out to the `claude` CLI already logged
in on this machine.

### 3. Check everything is wired up

```bash
uv run python -m app.doctor
```

If it complains the Gemini model ID is wrong, see what your key can reach:

```bash
uv run python -m app.list_models
```

### 4. Run

```bash
uv run python -m app.main
```

Then open <http://localhost:8765>.

---

## Using it

1. **Connect** — paste your YouTube Music request headers (the screen walks you
   through getting them). Saved to `browser.json`, valid ~2 years.
2. **Configure** — pick the source playlist, tick the destinations, and **write a
   one-line description for each**. This is the single biggest accuracy lever:
   "Slow Down" means nothing on its own, but "mellow, low-tempo, for winding down
   at the end of the day" gives the model something real to match against.
3. **Sort** — watch it work. Pause any time; nothing is lost.
4. **Review** — every song, grouped by destination, least-confident first. Click
   the chips to reassign. **Nothing reaches YouTube Music until you press Apply.**

---

## How it stays free

Songs are classified **50 at a time**, not one per request. A 2,000-song playlist
costs about 40 model calls; 10,000 songs costs about 200. Both fit inside the free
tier's daily quota, which makes the Claude fallback a rare safety net rather than
the normal path.

Everything expensive is cached in SQLite:

- Re-sorting a playlist you've already sorted costs **zero** model calls.
- A run interrupted at song 3,400 resumes at 3,400.
- Restarting the server doesn't lose an in-progress job.
- Changing a bucket's name or description correctly invalidates its cache.

---

## Safety

- **Only ever adds songs.** Your source playlist is never modified — no code path
  calls `remove_playlist_items`.
- **Backs up first.** Every destination playlist is dumped to `backups/` before any
  write.
- **Never double-adds.** Destinations are diffed against their live contents *and*
  against what this job already wrote, so re-running Apply is safe.
- **Writes are paced.** Chunks of 50, ~1.5s apart. Don't lower
  `YTM_WRITE_DELAY_SECONDS` — rapid writes through an unofficial client are what
  gets accounts flagged.

---

## Known limitations

- **`ytmusicapi` is unofficial.** It's outside YouTube's ToS and can break when
  Google changes internals. The official API was not an option: it charges 50 quota
  units per playlist insert against a 10,000/day allowance, i.e. 200 songs per day.
- **No audio analysis exists.** Spotify retired its energy/danceability endpoint for
  new apps in Nov 2024 and YouTube Music never had one. Classification rests on
  metadata, Last.fm tags, and the model's own knowledge. Well-known songs classify
  well; obscure tracks are weaker — which is what the confidence score and review
  screen are for.
- **Gemini free-tier prompts are used to improve Google's products.** Only song
  titles, artists, and tags are ever sent.
- **The Claude fallback consumes your Claude subscription limits**, so heavy sorting
  eats into the quota you use for other work.
- **YouTube Music caps playlists around 5,000 tracks** (10,000 for Liked Music).
  This app imposes no limit; the platform does.

---

## Layout

```
app/
  config.py      .env loading
  models.py      Track, Bucket, Assignment
  db.py          SQLite cache + job state
  ytm.py         YouTube Music read/write
  enrich.py      Last.fm tags
  classify.py    prompt building, batching, response validation
  router.py      Gemini -> Claude failover
  jobs.py        fetch -> enrich -> classify -> apply
  main.py        FastAPI server
  doctor.py      preflight check
  list_models.py which Gemini models your key can reach
web/             UI (no build step)
```
