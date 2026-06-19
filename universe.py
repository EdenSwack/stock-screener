"""Build the US-stock universe from Finnhub's symbol list.

Primary source: Finnhub /stock/symbol (one authenticated call) — all US-listed
COMMON stocks on NYSE / NASDAQ (mic in NYSE_NASDAQ_MICS). REITs drop out for free
because Finnhub types them 'REIT', not 'Common Stock'.

Falls back to the S&P 500 CSV only if the Finnhub call fails, so a transient error
degrades the run rather than killing it. Returns dicts so company name + exchange
flow through without a per-ticker profile2 call.
"""

from __future__ import annotations

import csv
import io
import logging

import requests

import finnhub_client as fh
from config import SP500_CSV, UNIVERSE_TYPE, NYSE_NASDAQ_MICS, MIC_TO_EXCHANGE

log = logging.getLogger("screener.universe")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioScreener/1.0)"}


def _from_finnhub() -> list[dict]:
    """All US common stocks on NYSE/NASDAQ from /stock/symbol (REITs excluded)."""
    data = fh.stock_symbols("US")
    if not data:
        return []
    out, dropped_type, dropped_mic = [], 0, 0
    for row in data:
        if row.get("type") != UNIVERSE_TYPE:  # excludes ETP / ADR / REIT / Unit / ...
            dropped_type += 1
            continue
        mic = (row.get("mic") or "").upper()
        if mic not in NYSE_NASDAQ_MICS:        # excludes OTC and ETF venues
            dropped_mic += 1
            continue
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append({"ticker": sym, "company": row.get("description") or None, "exchange": MIC_TO_EXCHANGE.get(mic, mic)})
    log.info(
        "Finnhub symbols: total=%d -> %d common NYSE/NASDAQ | dropped: type=%d mic=%d",
        len(data), len(out), dropped_type, dropped_mic,
    )
    return out


def _from_sp500() -> list[dict]:
    """Degraded fallback: S&P 500 constituents (symbols only; may include a few REITs)."""
    resp = requests.get(SP500_CSV, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    col = next((c for c in ("Symbol", "Ticker") if c in (reader.fieldnames or [])), None)
    if not col:
        return []
    return [
        {"ticker": r[col].strip().upper(), "company": r.get("Name"), "exchange": None}
        for r in reader
        if r.get(col, "").strip()
    ]


def build_universe() -> list[dict]:
    """Return the deduplicated list of {ticker, company, exchange}."""
    try:
        rows = _from_finnhub()
    except Exception as exc:  # noqa: BLE001 - never let universe build kill the run
        log.warning("Finnhub symbol fetch failed (%s)", exc)
        rows = []

    if not rows:
        log.warning("Finnhub universe empty — falling back to S&P 500 CSV")
        try:
            rows = _from_sp500()
            log.info("S&P 500 fallback -> %d tickers", len(rows))
        except Exception as exc:  # noqa: BLE001
            log.error("S&P 500 fallback also failed (%s)", exc)
            rows = []

    seen: set[str] = set()
    uniq: list[dict] = []
    for r in rows:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            uniq.append(r)
    uniq.sort(key=lambda r: r["ticker"])
    log.info("universe built: %d unique tickers", len(uniq))
    return uniq
