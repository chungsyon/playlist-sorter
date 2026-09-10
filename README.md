# Playlist Sorter

Sorts a YouTube Music playlist of any size into vibe-based destination playlists —
"Party Vibes", "Late Night Drive", "Slow Down", whatever buckets you've already made.
An LLM reads each song's metadata and decides where it belongs; you review every
assignment before a single track is written.

Runs entirely on your own machine. **Costs nothing to run**, on purpose: it uses
Gemini's free tier, and falls back to your Claude subscription only if that runs dry.

<!-- Screens: Connect · Configure · Sort · Review -->

---

## What you need

| | | |
|---|---|---|
| **Gemini API key** | Required | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — create it in a **new project** and **don't attach a billing account** |
| **Google OAuth client** | Required | [Cloud console](https://console.cloud.google.com/) — enable *YouTube Data API v3*, then create an OAuth client of type **TV and Limited Input devices** |
| **Last.fm key** | Optional | [last.fm/api/account/create](https://www.last.fm/api/account/create) — improves accuracy on obscure tracks |
| **`claude` CLI** | Optional | If installed and signed in, acts as a fallback when Gemini hits its quota |

Everything above is free. Nothing needs a credit card.

> **Why your own OAuth client rather than a shared one?** An unverified Google OAuth
> app is capped at 100 users and shows a warning screen to everyone else. Your own
> project has neither problem, because you're its owner.

Requires **Python 3.10+** and macOS or Linux.

---

## Install

```bash
git clone https://github.com/chungsyon/playlist-sorter.git
cd playlist-sorter
./setup.sh
```

`setup.sh` installs [uv](https://docs.astral.sh/uv/) if you don't have it, creates the
virtualenv, installs dependencies, and seeds your config. Safe to re-run.

Then start it:

```bash
uv run python -m app.main
```

Open <http://localhost:8765>. The app opens on **Setup**, where you paste your keys —
no editing config files by hand.

> **If your project folder is in iCloud Drive, Dropbox, or OneDrive**, `setup.sh`
> puts the virtualenv outside it automatically. Those services evict files that go
> cold, and an evicted virtualenv makes every `import` block on a download — the app
> appears to hang at 0% CPU with no output. Worth knowing if you ever move the folder.

---

## Using it

1. **Setup** — paste your Gemini key and OAuth client. Saved to `.env` locally.
2. **Connect** — sign in with Google. You get a short code, enter it on Google's
   device page, approve, and the app picks it up on its own. (There's also a
   paste-your-browser-headers path if you'd rather skip creating an OAuth client.)
3. **Configure** — pick the source playlist, tick the destinations, and **write a
   one-line description for each**. This is the single biggest accuracy lever:
   "Slow Down" means nothing on its own, but *"mellow, low-tempo, for winding down at
   the end of the day"* gives the model something real to match against.
4. **Sort** — watch it work. Pause any time; nothing is lost.
5. **Review** — every song, grouped by destination, least-confident first. Click the
   chips to reassign. **Nothing reaches YouTube Music until you press Apply.**

Check your setup at any time:

```bash
uv run python -m app.doctor
```

---

## How it stays free

Songs are classified **50 at a time**, not one per request. A 2,000-song playlist costs
about 40 model calls; 10,000 songs costs about 200. Both fit inside the free tier's
daily quota, which makes the Claude fallback a rare safety net rather than the normal path.

Everything expensive is cached in SQLite:

- Re-sorting a playlist you've already sorted costs **zero** model calls
- A run interrupted at song 3,400 resumes at 3,400
- Restarting the server doesn't lose an in-progress job
- Changing a bucket's name or description correctly invalidates its cache

**On billing:** a Google AI Pro/Ultra subscription does *not* cover API keys — it only
covers the AI Studio website. The free-tier key is what makes this free, and a project
with no billing account attached has no payment method to charge. Separately, the
`claude` CLI bills your subscription *unless* `ANTHROPIC_API_KEY` is set in your
environment, in which case it uses per-token API credits instead.

---

## Safety

- **Only ever adds songs.** Your source playlist is never modified — no code path calls
  `remove_playlist_items`.
- **Backs up first.** Every destination playlist is dumped to `backups/` before any write.
- **Never double-adds.** Destinations are diffed against their live contents *and*
  against what this job already wrote, so re-running Apply is safe.
- **Writes are paced.** Chunks of 50, ~1.5s apart. Don't lower `YTM_WRITE_DELAY_SECONDS`.
- **Credentials stay local.** `.env`, `oauth.json`, and `browser.json` are gitignored and
  never leave your machine.

---

## Known limitations

- **`ytmusicapi` is unofficial.** It's outside YouTube's Terms of Service and can break
  when Google changes internals. Using it carries some non-zero risk to your account.
  The official API was not a workable alternative: it charges 50 quota units per playlist
  insert against a 10,000/day allowance, i.e. **200 songs per day**. OAuth here supplies
  credentials only — requests still go to the internal endpoints, so there's no quota
  ceiling, and no verification review to pass.
- **No audio analysis exists.** Spotify retired its energy/danceability endpoint for new
  apps in Nov 2024 and YouTube Music never had one. Classification rests on metadata,
  Last.fm tags, and the model's own knowledge. Well-known songs classify well; obscure
  tracks are weaker — which is what the confidence score and review screen are for.
- **Gemini free-tier prompts are used to improve Google's products.** Only song titles,
  artists, albums, and tags are ever sent.
- **The Claude fallback consumes your Claude subscription limits**, so heavy sorting eats
  into the quota you use for other work.
- **YouTube Music caps playlists around 5,000 tracks** (10,000 for Liked Music). This app
  imposes no limit; the platform does.

---

## Layout

```
app/
  config.py      .env loading + runtime updates from the Setup screen
  models.py      Track, Bucket, Assignment
  db.py          SQLite cache + job state
  ytm.py         YouTube Music auth (OAuth + headers), read, write
  enrich.py      Last.fm tags
  classify.py    prompt building, batching, response validation
  router.py      Gemini -> Claude failover
  jobs.py        fetch -> enrich -> classify -> apply
  main.py        FastAPI server
  doctor.py      preflight check
  list_models.py which Gemini models your key can reach
web/             UI (no build step)
setup.sh         one-command install
```

---

## License

MIT — see [LICENSE](LICENSE).
