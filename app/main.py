"""FastAPI entrypoint.

Run with:  python -m app.main      (or via run.bat)
"""

from __future__ import annotations

import threading
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router as api_router
from .logging_setup import get_logger
from .models import JobState
from .paths import STATIC_DIR, ensure_dirs, load_settings
from .scraper.browser import manager

log = get_logger("main")


def reconcile_interrupted_jobs() -> None:
    """Startup recovery (Gap 5).

    Any job still marked RUNNING is by definition a job whose process died --
    nothing else could have left it in that state. Park it as PAUSED so it
    shows up in the UI with a Resume button instead of silently vanishing.
    """
    stale = db.resumable_jobs()
    for job in stale:
        if job["state"] == JobState.RUNNING.value:
            db.update_job(job["id"], state=JobState.PAUSED,
                          error="Interrupted by app restart")
            db.log(job["run_id"],
                   f"Job {job['id']} ({job['type']}) was interrupted; parked for resume",
                   level="warn")
    if stale:
        log.info("Reconciled %d interrupted job(s) on startup", len(stale))


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    db.init_db()
    reconcile_interrupted_jobs()
    log.info("PinClicks Mining Tool ready")
    yield
    await manager.shutdown()
    log.info("Shutting down")


app = FastAPI(title="PinClicks Mining Tool", version="1.0", lifespan=lifespan)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Serve the UI uncached.

    This is a local tool that is edited in place. Browser caching of app.js /
    style.css means an edit silently does nothing until a hard refresh, which
    looks exactly like a broken feature. Correctness beats a few saved bytes
    on localhost.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def main() -> None:
    settings = load_settings()
    port = int(settings.get("port", 8756))
    url = f"http://127.0.0.1:{port}"

    if settings.get("auto_open_browser", True):
        # Fire after a short delay so the server is accepting connections.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    log.info("Serving on %s", url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
