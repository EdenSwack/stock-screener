"""Since-first-appearance returns (backend/analysis only).

For each (screener_version, screener, ticker), take the FIRST date the stock appeared
in ``screener_history`` and record its close then vs the latest close, plus SPY over
the same window, into ``screener_since_entry``. Unlike the fixed-horizon labeller this
needs no horizon to mature: every pick is measured to today, and the app normalizes by
days held (monthly-equivalent excess over SPY). Rebuilt in full each run.
"""

from __future__ import annotations

import bisect
import logging
import math

import requests

from forward_returns import SUPABASE_URL, SERVICE_ROLE_KEY, _entry_index, _fetch_path, _get_all, _headers

log = logging.getLogger("screener.since_entry")


def run() -> None:
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        log.info("SUPABASE_* not set — skipping since-entry run")
        return

    history = _get_all("screener_history", "run_date,ticker,screener,risk_gate,screener_version,peg")
    if not history:
        log.info("no screener_history rows yet")
        return

    # First appearance per (version, screener, ticker).
    first: dict[tuple[str, str, str], dict] = {}
    for h in sorted(history, key=lambda r: r["run_date"]):
        key = (h.get("screener_version") or "unversioned", h["screener"], h["ticker"])
        first.setdefault(key, h)
    log.info("%d first appearances across %d tickers", len(first), len({k[2] for k in first}))

    spy = _fetch_path("SPY")
    if not spy:
        log.error("could not fetch SPY path — aborting")
        return
    spy_dates, spy_closes = spy

    paths: dict[str, tuple[list[str], list[float]] | None] = {}
    rows: list[dict] = []
    for (version, screener, ticker), h in first.items():
        if ticker not in paths:
            paths[ticker] = _fetch_path(ticker)
        path = paths[ticker]
        if not path:
            continue
        dates, closes = path
        ei = _entry_index(dates, h["run_date"])
        if ei is None or not closes[ei]:
            continue
        # SPY on the stock's own entry / last dates (last SPY close on or before each).
        si = bisect.bisect_right(spy_dates, dates[ei]) - 1
        sl = bisect.bisect_right(spy_dates, dates[-1]) - 1
        if si < 0 or sl < 0:
            continue
        vals = [closes[ei], closes[-1], spy_closes[si], spy_closes[sl]]
        if not all(math.isfinite(v) for v in vals):
            continue
        peg = h.get("peg")
        rows.append({
            "screener_version": version, "screener": screener, "ticker": ticker,
            "entry_date": dates[ei], "entry_price": closes[ei],
            "mkt_entry": spy_closes[si], "asof_date": dates[-1], "last_price": closes[-1],
            "mkt_last": spy_closes[sl], "days_held": len(dates) - 1 - ei,
            "risk_gate": h.get("risk_gate"),
            "peg": float(peg) if peg is not None and math.isfinite(float(peg)) else None,
        })

    for j in range(0, len(rows), 500):
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/screener_since_entry",
            params={"on_conflict": "screener_version,screener,ticker"},
            headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
            json=rows[j:j + 500],
            timeout=60,
        )
        r.raise_for_status()
    log.info("wrote %d since-entry row(s)", len(rows))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
