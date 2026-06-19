"""Publish the latest results to Supabase so the frontend can read them there.

Writes a single row into ``screener_cache`` (id=1) via PostgREST using the
service-role key, which bypasses RLS. If the SUPABASE_* env vars are absent this
no-ops with a log line, so local runs (which still write results.json) don't fail.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("screener.publish")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def to_supabase(results: dict) -> None:
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        log.info("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — skipping publish")
        return

    row = {
        "id": 1,  # single-row cache
        "data": results,
        "updated_at": results["last_updated"],
    }
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/screener_cache",
            params={"on_conflict": "id"},
            headers={
                "apikey": SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json=row,
            timeout=30,
        )
        resp.raise_for_status()
        log.info("published results to Supabase screener_cache")
    except Exception as exc:  # noqa: BLE001 - publish failure must not fail the run
        log.error("Supabase publish failed: %s", exc)


def history_to_supabase(results: dict) -> None:
    """Append this run's screen membership to ``screener_history`` (one row per
    ticker × screener). Idempotent per day: re-running the same date upserts on the
    (run_date, ticker, screener) PK rather than duplicating."""
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        log.info("SUPABASE_* not set — skipping history publish")
        return

    run_date = results["last_updated"][:10]  # YYYY-MM-DD
    rows = [
        {
            "run_date": run_date,
            "ticker": s["ticker"],
            "screener": screener,
            "composite_score": s.get("composite_score"),
            "price": s.get("price"),
        }
        for screener, stocks in results["screeners"].items()
        for s in stocks
    ]
    if not rows:
        log.info("no screen matches — nothing to write to history")
        return

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/screener_history",
            params={"on_conflict": "run_date,ticker,screener"},
            headers={
                "apikey": SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json=rows,
            timeout=30,
        )
        resp.raise_for_status()
        log.info("published %d row(s) to Supabase screener_history (run_date=%s)", len(rows), run_date)
    except Exception as exc:  # noqa: BLE001 - history publish failure must not fail the run
        log.error("Supabase history publish failed: %s", exc)
