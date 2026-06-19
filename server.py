"""FastAPI server: read endpoint, manual refresh, and the nightly scheduler.

Endpoints
  GET  /api/screener          -> cached results (4 screener lists + last_updated)
  POST /api/screener/refresh  -> kick off a re-run in the background (202)
  GET  /api/screener/health   -> liveness + whether a run is in progress

The nightly job runs at 23:00 Asia/Jerusalem (see config.SCHEDULE_*).
Runs are serialized by a single in-process lock — a manual refresh while the
nightly job is running (or vice-versa) is rejected rather than run concurrently,
since the Finnhub rate limiter assumes one run at a time.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import pipeline
from config import SCHEDULE_TZ, SCHEDULE_HOUR, SCHEDULE_MINUTE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("screener.server")

app = FastAPI(title="Portfolio Screener")

# Frontend is a separate origin (Vite dev server / deployed SPA). Tighten the
# allow_origins list to your real domains before production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_run_lock = threading.Lock()
_is_running = threading.Event()


def _run_job(trigger: str) -> None:
    """Run the pipeline under the single-run lock. Skips if one is already going."""
    if not _run_lock.acquire(blocking=False):
        log.warning("refresh requested via %s but a run is already in progress — skipped", trigger)
        return
    _is_running.set()
    try:
        log.info("starting screener run (trigger=%s)", trigger)
        pipeline.run()
    except Exception:  # noqa: BLE001 - never let a failed run kill the scheduler thread
        log.exception("screener run failed (trigger=%s)", trigger)
    finally:
        _is_running.clear()
        _run_lock.release()


@app.get("/api/screener")
def get_screener():
    results = pipeline.load_results()
    if results is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No results yet. Trigger /api/screener/refresh or wait for the nightly run."},
        )
    return results


@app.post("/api/screener/refresh")
def refresh_screener():
    if _is_running.is_set():
        return JSONResponse(status_code=409, content={"status": "already_running"})
    threading.Thread(target=_run_job, args=("manual",), daemon=True).start()
    return JSONResponse(status_code=202, content={"status": "started"})


@app.get("/api/screener/health")
def health():
    return {"ok": True, "running": _is_running.is_set()}


scheduler = BackgroundScheduler(timezone=SCHEDULE_TZ)
scheduler.add_job(
    lambda: _run_job("schedule"),
    "cron",
    hour=SCHEDULE_HOUR,
    minute=SCHEDULE_MINUTE,
    id="nightly_screener",
)


@app.on_event("startup")
def _start_scheduler():
    scheduler.start()
    log.info("scheduler started — nightly run at %02d:%02d %s",
             SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TZ)


@app.on_event("shutdown")
def _stop_scheduler():
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8787)
