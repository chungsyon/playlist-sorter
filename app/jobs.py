"""Job runner: fetch -> enrich -> classify -> (review) -> apply.

Design rules that matter:
  * Nothing is written to YouTube Music until apply() is called explicitly.
  * Classification results are cached by (video_id, bucket_hash), so a resumed
    or repeated run costs zero model calls for songs already done.
  * Running out of quota pauses the job, it does not fail it.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import classify, db, enrich, ytm
from .config import settings
from .models import Assignment, Bucket, Track, bucket_hash, group_names, missing_groups
from .router import AllProvidersExhausted, ProviderRouter

log = logging.getLogger(__name__)


@dataclass
class JobState:
    job_id: str
    source_playlist_id: str
    source_name: str = ""
    buckets: list[Bucket] = field(default_factory=list)
    bucket_hash: str = ""

    phase: str = "pending"       # pending|fetching|enriching|classifying|
                                 # classified|applying|done|paused|error
    message: str = ""
    total: int = 0
    done: int = 0
    error: str = ""

    tracks: list[Track] = field(default_factory=list)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    router: ProviderRouter = field(default_factory=ProviderRouter)

    applied_counts: dict[str, int] = field(default_factory=dict)
    failed_writes: list[str] = field(default_factory=list)
    backup_path: str = ""

    _stop: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "phase": self.phase,
            "message": self.message,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "source_name": self.source_name,
            "buckets": [b.model_dump() for b in self.buckets],
            "providers": self.router.status(),
            "applied_counts": self.applied_counts,
            "failed_writes": len(self.failed_writes),
            "backup_path": self.backup_path,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> JobState | None:
        state = self._jobs.get(job_id)
        if state is None:
            state = self._restore(job_id)
        return state

    def _restore(self, job_id: str) -> JobState | None:
        """Rebuild a job from SQLite after a server restart.

        Everything expensive was cached as it was produced, so this costs no
        model calls and no YouTube Music reads — a long sort survives a restart.
        """
        row = db.get_job(job_id)
        if not row:
            return None

        buckets = [Bucket(**b) for b in json.loads(row["buckets"])]
        track_ids = json.loads(row.get("track_ids") or "[]")
        tracks_by_id = db.get_tracks(track_ids)
        tracks = [tracks_by_id[v] for v in track_ids if v in tracks_by_id]

        for vid, tags in db.get_cached_tags(track_ids).items():
            if vid in tracks_by_id:
                tracks_by_id[vid].tags = tags

        state = JobState(
            job_id=job_id,
            source_playlist_id=row["source_playlist_id"],
            source_name=row["source_name"] or "",
            buckets=buckets,
            bucket_hash=row["bucket_hash"],
            phase=row["status"],
            message="Restored from disk.",
            total=row["total"] or len(tracks),
            done=row["classified"] or 0,
            tracks=tracks,
            assignments=db.get_classifications(row["bucket_hash"], track_ids),
        )
        state.router.tallies = json.loads(row["provider_tallies"] or "{}")

        with self._lock:
            self._jobs[job_id] = state
        log.info("Restored job %s (%d tracks, %d classified)",
                 job_id, len(tracks), len(state.assignments))
        return state

    def create(self, source_playlist_id: str, buckets: list[Bucket]) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        state = JobState(
            job_id=job_id,
            source_playlist_id=source_playlist_id,
            buckets=buckets,
            # The prompt is part of the key: edit the wording and cached
            # results from the old wording stop being reused.
            bucket_hash=bucket_hash(buckets, classify.rules_for(buckets)),
        )
        with self._lock:
            self._jobs[job_id] = state
        db.create_job(
            job_id, source_playlist_id, "",
            json.dumps([b.model_dump() for b in buckets]),
            state.bucket_hash, 0,
        )
        return state

    def start(self, job_id: str) -> None:
        state = self._jobs[job_id]
        if job_id in self._threads and self._threads[job_id].is_alive():
            return
        state._stop.clear()
        thread = threading.Thread(target=self._run, args=(state,),
                                  name=f"job-{job_id}", daemon=True)
        self._threads[job_id] = thread
        thread.start()

    def pause(self, job_id: str) -> None:
        state = self._jobs.get(job_id)
        if state:
            state._stop.set()
            state.message = "Pausing after the current batch..."

    # ----------------------------------------------------------------------
    # classification pipeline
    # ----------------------------------------------------------------------

    def _run(self, state: JobState) -> None:
        try:
            self._fetch(state)
            if state._stop.is_set():
                return self._pause(state, "Paused before classifying.")

            self._enrich(state)
            if state._stop.is_set():
                return self._pause(state, "Paused before classifying.")

            self._classify(state)

        except AllProvidersExhausted as e:
            # Not a failure — the cursor is intact and cached work is saved.
            self._pause(state, f"Out of quota on every provider. Resume later. ({e})")
        except Exception as e:  # noqa: BLE001
            log.exception("Job %s failed", state.job_id)
            state.phase, state.error = "error", str(e)
            state.message = f"Failed: {e}"
            db.update_job(state.job_id, status="error", error=str(e))

    def _fetch(self, state: JobState) -> None:
        state.phase = "fetching"
        state.message = "Reading the source playlist..."
        db.update_job(state.job_id, status="fetching")

        name, tracks = ytm.fetch_all_tracks(state.source_playlist_id)
        state.source_name = name
        state.tracks = tracks
        state.total = len(tracks)
        state.message = f"Found {len(tracks)} songs in “{name}”."

        db.upsert_tracks(tracks)
        db.update_job(state.job_id, source_name=name, total=len(tracks),
                      track_ids=json.dumps([t.video_id for t in tracks]))

    def _enrich(self, state: JobState) -> None:
        if not settings.has_lastfm:
            return
        state.phase = "enriching"
        state.done = 0
        db.update_job(state.job_id, status="enriching")

        def progress(done: int, total: int) -> None:
            state.done = done
            state.total = total
            state.message = f"Fetching Last.fm tags... {done}/{total}"

        enrich.enrich(state.tracks, on_progress=progress,
                      should_stop=state._stop.is_set)

    def _classify(self, state: JobState) -> None:
        state.phase = "classifying"
        db.update_job(state.job_id, status="classifying")

        # Anything already classified against this exact bucket set is free.
        cached = db.get_classifications(
            state.bucket_hash, [t.video_id for t in state.tracks]
        )
        state.assignments.update(cached)

        pending = [t for t in state.tracks if t.video_id not in state.assignments]
        state.total = len(state.tracks)
        state.done = len(state.assignments)

        if cached:
            state.message = (f"{len(cached)} songs already classified from a "
                             f"previous run — {len(pending)} to go.")

        for batch in classify.batched(pending, settings.batch_size):
            if state._stop.is_set():
                return self._pause(state, "Paused. Resume to continue.")

            results = classify.classify_batch(state.router, batch, state.buckets)

            db.save_classifications(state.bucket_hash, results)
            for a in results:
                state.assignments[a.video_id] = a

            state.done = len(state.assignments)
            state.message = f"Classified {state.done}/{state.total} songs."
            db.update_job(state.job_id, classified=state.done,
                          provider_tallies=json.dumps(state.router.tallies))

        state.phase = "classified"
        state.message = f"Done. {state.total} songs ready for review."
        db.update_job(state.job_id, status="classified", classified=state.done)

    def _pause(self, state: JobState, message: str) -> None:
        state.phase = "paused"
        state.message = message
        db.update_job(state.job_id, status="paused", classified=state.done)

    # ----------------------------------------------------------------------
    # review + apply
    # ----------------------------------------------------------------------

    def summary_payload(self, state: JobState) -> dict[str, Any]:
        """What was actually written, after the fact.

        Built from the `applied` table rather than from the assignments, so it
        reports what genuinely reached YouTube Music — songs already present in
        a destination were skipped and must not appear here as if we added them.
        """
        applied = db.get_applied(state.job_id)          # {(playlist_id, video_id)}
        overrides = db.get_overrides(state.job_id)
        tracks_by_id = {t.video_id: t for t in state.tracks}

        written: dict[str, list[str]] = {}
        for playlist_id, video_id in applied:
            written.setdefault(playlist_id, []).append(video_id)

        # What we intended, so "already there" can be separated from "failed".
        planned: dict[str, set[str]] = {}
        by_name = {b.name: b for b in state.buckets}
        for vid, assignment in state.assignments.items():
            for name in overrides.get(vid, assignment.playlists):
                bucket = by_name.get(name)
                if bucket:
                    planned.setdefault(bucket.playlist_id, set()).add(vid)

        failed = set(state.failed_writes)
        buckets: list[dict[str, Any]] = []
        total_added = total_skipped = 0

        for bucket in state.buckets:
            ids = written.get(bucket.playlist_id, [])
            songs = []
            for vid in ids:
                track = tracks_by_id.get(vid)
                assignment = state.assignments.get(vid)
                songs.append({
                    "video_id": vid,
                    "title": track.title if track else vid,
                    "artists": track.artists if track else "",
                    "confidence": round(assignment.confidence, 2) if assignment else None,
                    "reason": assignment.reason if assignment else "",
                    "overridden": vid in overrides,
                })
            songs.sort(key=lambda s: (s["artists"].lower(), s["title"].lower()))

            intended = planned.get(bucket.playlist_id, set())
            skipped = len(intended) - len(ids) - len(intended & failed)

            total_added += len(ids)
            total_skipped += max(0, skipped)

            buckets.append({
                "playlist_id": bucket.playlist_id,
                "name": bucket.name,
                "description": bucket.description,
                "group": bucket.group,
                "added": len(ids),
                "skipped": max(0, skipped),
                "songs": songs,
            })

        # Grouped sorts read better by axis — all the "Use" playlists together,
        # then all the "Vibe" ones. Busiest first inside each group, as before.
        order = {name: i for i, name in enumerate(group_names(state.buckets))}
        buckets.sort(key=lambda b: (order.get(b["group"], len(order)), -b["added"]))

        unassigned = incomplete = 0
        for vid, a in state.assignments.items():
            names = overrides.get(vid, a.playlists)
            if not names:
                unassigned += 1
            elif missing_groups(names, state.buckets):
                incomplete += 1

        return {
            "job_id": state.job_id,
            "source_name": state.source_name,
            "phase": state.phase,
            "groups": group_names(state.buckets),
            "buckets": buckets,
            "backup_path": state.backup_path,
            "stats": {
                "added": total_added,
                "playlists": sum(1 for b in buckets if b["added"]),
                "skipped": total_skipped,
                "failed": len(state.failed_writes),
                "unassigned": unassigned,
                "incomplete": incomplete,
                "classified": len(state.assignments),
            },
        }

    def review_payload(self, state: JobState) -> dict[str, Any]:
        """Everything the review screen needs, grouped by destination playlist."""
        overrides = db.get_overrides(state.job_id)
        tracks_by_id = {t.video_id: t for t in state.tracks}

        by_bucket: dict[str, list[dict]] = {b.name: [] for b in state.buckets}
        unmatched: list[dict] = []
        incomplete: list[dict] = []
        low_confidence = 0

        for vid, assignment in state.assignments.items():
            track = tracks_by_id.get(vid)
            if not track:
                continue

            names = overrides.get(vid, assignment.playlists)
            gaps = missing_groups(names, state.buckets)
            row = {
                "video_id": vid,
                "title": track.title,
                "artists": track.artists,
                "tags": track.tags,
                "playlists": names,
                "confidence": round(assignment.confidence, 2),
                "reason": assignment.reason,
                "provider": assignment.provider,
                "overridden": vid in overrides,
                "low_confidence": assignment.confidence < settings.confidence_threshold,
                "missing_groups": gaps,
            }
            if row["low_confidence"]:
                low_confidence += 1

            if not names:
                unmatched.append(row)
            elif gaps:
                # Landed somewhere, but a group it was promised to came up empty.
                # Easy to miss inside a bucket list, so it gets its own section.
                incomplete.append(row)
            for name in names:
                by_bucket.setdefault(name, []).append(row)

        # Least-certain first — that's where human attention is worth spending.
        for rows in by_bucket.values():
            rows.sort(key=lambda r: r["confidence"])
        unmatched.sort(key=lambda r: r["confidence"])
        incomplete.sort(key=lambda r: r["confidence"])

        return {
            "job_id": state.job_id,
            "source_name": state.source_name,
            "groups": group_names(state.buckets),
            "buckets": [
                {
                    "playlist_id": b.playlist_id,
                    "name": b.name,
                    "description": b.description,
                    "group": b.group,
                    "songs": by_bucket.get(b.name, []),
                }
                for b in state.buckets
            ],
            "unmatched": unmatched,
            "incomplete": incomplete,
            "stats": {
                "total": len(state.assignments),
                "assigned": len(state.assignments) - len(unmatched),
                "unmatched": len(unmatched),
                "incomplete": len(incomplete),
                "low_confidence": low_confidence,
                "providers": state.router.tallies,
            },
        }

    def apply(self, job_id: str) -> None:
        state = self._jobs[job_id]
        thread = threading.Thread(target=self._apply, args=(state,),
                                  name=f"apply-{job_id}", daemon=True)
        self._threads[f"apply-{job_id}"] = thread
        thread.start()

    def _apply(self, state: JobState) -> None:
        """Write to YouTube Music. Only ever adds — the source is never touched."""
        try:
            state.phase = "applying"
            state.message = "Backing up destination playlists..."
            db.update_job(state.job_id, status="applying")

            by_name = {b.name: b for b in state.buckets}
            overrides = db.get_overrides(state.job_id)

            planned: dict[str, list[str]] = {}
            for vid, assignment in state.assignments.items():
                for name in overrides.get(vid, assignment.playlists):
                    bucket = by_name.get(name)
                    if bucket:
                        planned.setdefault(bucket.playlist_id, []).append(vid)

            if not planned:
                state.phase = "done"
                state.message = "Nothing to apply — no songs were assigned."
                db.update_job(state.job_id, status="done")
                return

            state.backup_path = str(
                ytm.snapshot_playlists(list(planned), f"job-{state.job_id}")
            )

            already = db.get_applied(state.job_id)
            total_added = 0

            for playlist_id, video_ids in planned.items():
                if state._stop.is_set():
                    return self._pause(state, "Paused mid-apply. Resume to finish.")

                name = next((b.name for b in state.buckets
                             if b.playlist_id == playlist_id), playlist_id)
                state.message = f"Reading current contents of “{name}”..."

                # Two-layer duplicate guard: what's live in the playlist, and
                # what we already wrote for this job.
                existing = ytm.get_playlist_video_ids(playlist_id)
                to_add = [
                    v for v in dict.fromkeys(video_ids)
                    if v not in existing and (playlist_id, v) not in already
                ]

                if not to_add:
                    state.applied_counts[name] = 0
                    continue

                state.message = f"Adding {len(to_add)} songs to “{name}”..."
                added, failed = ytm.add_tracks(playlist_id, to_add)

                written = [v for v in to_add if v not in failed]
                db.mark_applied(state.job_id, playlist_id, written)

                state.applied_counts[name] = added
                state.failed_writes.extend(failed)
                total_added += added

            state.phase = "done"
            state.message = f"Applied. {total_added} songs added across {len(planned)} playlists."
            if state.failed_writes:
                state.message += f" {len(state.failed_writes)} failed — safe to re-run Apply."
            db.update_job(state.job_id, status="done")

        except Exception as e:  # noqa: BLE001
            log.exception("Apply failed for job %s", state.job_id)
            state.phase, state.error = "error", str(e)
            state.message = f"Apply failed: {e}"
            db.update_job(state.job_id, status="error", error=str(e))


manager = JobManager()
