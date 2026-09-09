"""Local FastAPI server. Runs on localhost only — nothing is exposed."""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, ytm
from .config import ROOT, settings
from .jobs import manager
from .models import Bucket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("playlist-sorter")

WEB_DIR = ROOT / "web"

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    log.info("Playlist Sorter ready at http://localhost:%d", settings.port)
    yield


app = FastAPI(title="Playlist Sorter", docs_url=None, redoc_url=None, lifespan=lifespan)


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------

class AuthBody(BaseModel):
    headers_raw: str


class BucketBody(BaseModel):
    playlist_id: str
    name: str
    description: str = ""


class JobBody(BaseModel):
    source_playlist_id: str
    buckets: list[BucketBody]


class OverrideBody(BaseModel):
    video_id: str
    playlists: list[str]


# --------------------------------------------------------------------------
# status + auth
# --------------------------------------------------------------------------

@app.get("/api/status")
def status() -> dict:
    ytm_ok, ytm_msg = (ytm.check_auth() if settings.ytm_authenticated
                       else (False, "Not connected yet."))
    return {
        "gemini": {"configured": settings.has_gemini, "model": settings.gemini_model},
        "claude": {
            "configured": settings.claude_enabled
            and shutil.which(settings.claude_cli_path) is not None,
            "model": settings.claude_model,
        },
        "lastfm": {"configured": settings.has_lastfm},
        "ytmusic": {"configured": ytm_ok, "message": ytm_msg},
        "batch_size": settings.batch_size,
    }


@app.post("/api/auth/ytm")
def auth_ytm(body: AuthBody) -> dict:
    try:
        ytm.setup_auth(body.headers_raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not save credentials: {e}") from e

    ok, message = ytm.check_auth()
    if not ok:
        raise HTTPException(400, message)
    return {"ok": True, "message": message}


@app.get("/api/playlists")
def playlists() -> dict:
    try:
        return {"playlists": ytm.list_playlists()}
    except ytm.NotAuthenticated as e:
        raise HTTPException(401, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not list playlists: {e}") from e


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@app.post("/api/jobs")
def create_job(body: JobBody) -> dict:
    if not body.buckets:
        raise HTTPException(400, "Pick at least one destination playlist.")

    names = [b.name.strip().lower() for b in body.buckets]
    if len(set(names)) != len(names):
        raise HTTPException(400, "Destination playlists must have distinct names.")
    if any(b.playlist_id == body.source_playlist_id for b in body.buckets):
        raise HTTPException(400, "A destination cannot be the source playlist.")

    buckets = [Bucket(playlist_id=b.playlist_id, name=b.name.strip(),
                      description=b.description.strip()) for b in body.buckets]

    state = manager.create(body.source_playlist_id, buckets)
    manager.start(state.job_id)
    return {"job_id": state.job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    state = manager.get(job_id)
    if not state:
        raise HTTPException(404, "Unknown job.")
    return state.snapshot()


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    if not manager.get(job_id):
        raise HTTPException(404, "Unknown job.")
    manager.pause(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    state = manager.get(job_id)
    if not state:
        raise HTTPException(404, "Unknown job.")
    # A fresh router clears the exhausted flags — quota may have reset.
    from .router import ProviderRouter
    state.router = ProviderRouter()
    manager.start(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/review")
def review(job_id: str) -> dict:
    state = manager.get(job_id)
    if not state:
        raise HTTPException(404, "Unknown job.")
    return manager.review_payload(state)


@app.post("/api/jobs/{job_id}/override")
def override(job_id: str, body: OverrideBody) -> dict:
    state = manager.get(job_id)
    if not state:
        raise HTTPException(404, "Unknown job.")

    valid = {b.name for b in state.buckets}
    unknown = [p for p in body.playlists if p not in valid]
    if unknown:
        raise HTTPException(400, f"Unknown playlist(s): {', '.join(unknown)}")

    db.save_override(job_id, body.video_id, body.playlists)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/apply")
def apply_job(job_id: str) -> dict:
    state = manager.get(job_id)
    if not state:
        raise HTTPException(404, "Unknown job.")
    if state.phase not in {"classified", "paused", "done", "error"}:
        raise HTTPException(400, f"Job is still {state.phase}. Wait for it to finish.")

    state._stop.clear()
    manager.apply(job_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# static UI
# --------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def run() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
