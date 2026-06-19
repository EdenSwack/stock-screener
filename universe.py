"""Build the US-stock universe from three free static lists and deduplicate.

Resilient by design: if any single source fails (iShares in particular likes to
block non-browser clients), it is logged and skipped rather than aborting the run.
"""

from __future__ import annotations

import csv
import io
import logging

import requests

from config import SP500_CSV, NASDAQ100_CSV, RUSSELL2000_CSV

log = logging.getLogger("screener.universe")

# A browser-ish UA — iShares 403s the default python-requests UA.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioScreener/1.0)"}


def _get(url: str) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _tickers_from_simple_csv(text: str, symbol_cols=("Symbol", "Ticker")) -> list[str]:
    """Parse a clean CSV whose first row is the header (S&P 500, Nasdaq-100)."""
    reader = csv.DictReader(io.StringIO(text))
    col = next((c for c in symbol_cols if c in (reader.fieldnames or [])), None)
    if not col:
        log.warning("no symbol column found; headers=%s", reader.fieldnames)
        return []
    return [row[col].strip().upper() for row in reader if row.get(col, "").strip()]


def _tickers_from_ishares_csv(text: str) -> list[str]:
    """iShares holdings CSVs have a metadata preamble before the real header row;
    skip lines until we find the one starting with 'Ticker'."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lstrip('"').startswith("Ticker")), None)
    if start is None:
        log.warning("could not locate 'Ticker' header in iShares CSV")
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for row in reader:
        sym = (row.get("Ticker") or "").strip().upper()
        # Holdings files include cash/derivative rows with blank or non-equity tickers.
        if sym and sym.isalpha():
            out.append(sym)
    return out


def _safe_source(name: str, fn) -> list[str]:
    try:
        tickers = fn()
        log.info("source %s -> %d tickers", name, len(tickers))
        return tickers
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
        log.warning("source %s failed (%s) — skipping", name, exc)
        return []


def build_universe() -> list[str]:
    """Return the deduplicated, sorted list of unique US tickers."""
    collected: set[str] = set()
    collected.update(_safe_source("sp500", lambda: _tickers_from_simple_csv(_get(SP500_CSV))))
    collected.update(_safe_source("nasdaq100", lambda: _tickers_from_simple_csv(_get(NASDAQ100_CSV))))
    collected.update(_safe_source("russell2000", lambda: _tickers_from_ishares_csv(_get(RUSSELL2000_CSV))))

    universe = sorted(collected)
    log.info("universe built: %d unique tickers", len(universe))
    return universe
