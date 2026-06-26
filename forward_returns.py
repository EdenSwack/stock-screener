"""Forward-returns labeller (backend/analysis only).

For each screen snapshot in ``screener_history``, fill ``screener_forward_returns``
with what actually happened afterwards — at 21/63/126/252 trading days:
  • raw buy-and-hold return,
  • ATR-chandelier stop-adjusted return (the tradeable strategy), and
  • the S&P 500 return over the same window (for excess-over-market).

A horizon is "matured" once we have at least that many trading bars after the
screen date. Daily prices come from the Yahoo proxy (no API cap). Idempotent:
re-running only fills newly-matured horizons; fully-filled rows are skipped (and
their tickers never re-fetched). Runs as its own GitHub Actions cron.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("screener.forward_returns")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PROXY = "https://finhub-ticker-proxy.edenswack1.workers.dev"

HORIZONS = {"21d": 21, "63d": 63, "126d": 126, "252d": 252}
ATR_K = 3.0  # chandelier multiplier (matches the app's stop)
_THROTTLE_S = 0.15  # be polite to the proxy


def _headers() -> dict:
    return {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _get_all(table: str, select: str) -> list[dict]:
    """Paginated GET of a whole table (PostgREST caps at 1000/req)."""
    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"select": select},
            headers={**_headers(), "Range-Unit": "items", "Range": f"{offset}-{offset + 999}"},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def _fetch_path(symbol: str) -> tuple[list[str], list[float]] | None:
    """Daily (date, close) series for the symbol from the Yahoo proxy, or None."""
    try:
        time.sleep(_THROTTLE_S)
        r = requests.get(f"{PROXY}/", params={"symbol": symbol, "range": "2y", "interval": "1d"}, timeout=30)
        if not r.ok:
            return None
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        dates, out = [], []
        for t, c in zip(ts, closes):
            if c is not None:
                dates.append(time.strftime("%Y-%m-%d", time.gmtime(t)))
                out.append(float(c))
        return (dates, out) if dates else None
    except Exception as exc:  # noqa: BLE001 - a bad fetch must not break the run
        log.warning("path fetch failed for %s: %s", symbol, exc)
        return None


def _entry_index(dates: list[str], run_date: str) -> int | None:
    for i, d in enumerate(dates):
        if d >= run_date:
            return i
    return None


def _stop_return(path: list[float], atr: float | None) -> float | None:
    """Chandelier stop: exit at the first close <= (running high − k·ATR)."""
    if not atr or atr <= 0 or len(path) < 2:
        return None
    entry = path[0]
    high = entry
    for px in path[1:]:
        high = max(high, px)
        if px <= high - ATR_K * atr:
            return px / entry - 1
    return path[-1] / entry - 1


def run() -> None:
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        log.info("SUPABASE_* not set — skipping forward-returns run")
        return

    history = _get_all("screener_history", "run_date,ticker,price,atr")
    if not history:
        log.info("no screener_history rows yet")
        return

    # Dedupe to one (run_date, ticker); price/atr are identical across screeners.
    snaps: dict[tuple[str, str], dict] = {}
    for h in history:
        snaps.setdefault((h["run_date"], h["ticker"]), {"price": h.get("price"), "atr": h.get("atr")})

    existing = {
        (e["run_date"], e["ticker"]): e
        for e in _get_all("screener_forward_returns", "run_date,ticker," + ",".join(f"ret_{k}" for k in HORIZONS))
    }

    # Which (date,ticker) still have an unfilled horizon? Group the work by ticker.
    pending_by_ticker: dict[str, list[str]] = {}
    for (run_date, ticker), _ in snaps.items():
        e = existing.get((run_date, ticker))
        if e and all(e.get(f"ret_{k}") is not None for k in HORIZONS):
            continue  # fully filled — never touch again
        pending_by_ticker.setdefault(ticker, []).append(run_date)

    if not pending_by_ticker:
        log.info("nothing pending — all snapshots fully labelled")
        return
    log.info("pending: %d ticker(s), %d (date,ticker) snapshot(s)",
             len(pending_by_ticker), sum(len(v) for v in pending_by_ticker.values()))

    spy = _fetch_path("SPY")
    if not spy:
        log.error("could not fetch SPY path — aborting (needed for market returns)")
        return
    spy_dates, spy_closes = spy

    upserts: list[dict] = []
    for i, (ticker, run_dates) in enumerate(sorted(pending_by_ticker.items()), 1):
        path = _fetch_path(ticker)
        if not path:
            continue
        dates, closes = path
        for run_date in run_dates:
            ei = _entry_index(dates, run_date)
            si = _entry_index(spy_dates, run_date)
            if ei is None:
                continue
            atr = snaps[(run_date, ticker)].get("atr")
            row: dict = {"run_date": run_date, "ticker": ticker, "entry_price": closes[ei]}
            filled = False
            for key, n in HORIZONS.items():
                if ei + n < len(closes):
                    row[f"ret_{key}"] = closes[ei + n] / closes[ei] - 1
                    row[f"stop_ret_{key}"] = _stop_return(closes[ei:ei + n + 1], atr)
                    if si is not None and si + n < len(spy_closes):
                        row[f"mkt_ret_{key}"] = spy_closes[si + n] / spy_closes[si] - 1
                    filled = True
            if filled:
                upserts.append(row)
        if i % 25 == 0:
            log.info("processed %d/%d tickers", i, len(pending_by_ticker))

    if not upserts:
        log.info("no matured horizons to write yet")
        return

    # Upsert in chunks.
    for j in range(0, len(upserts), 500):
        chunk = upserts[j:j + 500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/screener_forward_returns",
            params={"on_conflict": "run_date,ticker"},
            headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
            json=chunk,
            timeout=60,
        )
        r.raise_for_status()
    log.info("wrote %d forward-return row(s)", len(upserts))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
