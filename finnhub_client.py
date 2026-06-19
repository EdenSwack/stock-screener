"""Throttled Finnhub client + metric extraction/normalization.

Only free-tier endpoints are used: /quote, /stock/profile2, /stock/metric.
Every screener field — including EV/EBITDA and FCF growth — comes from the free
/stock/metric payload, so there is no second data source and no per-ticker
enrichment call.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from config import (
    FINNHUB_API_KEY,
    FINNHUB_BASE,
    FINNHUB_RATE_LIMIT_PER_MIN,
    is_nyse_or_nasdaq,
)

log = logging.getLogger("screener.finnhub")

_MIN_INTERVAL = 60.0 / FINNHUB_RATE_LIMIT_PER_MIN  # seconds between calls
_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """Serialize calls so we never exceed FINNHUB_RATE_LIMIT_PER_MIN."""
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _get(path: str, params: dict) -> dict | None:
    _throttle()
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}{path}",
            params={**params, "token": FINNHUB_API_KEY},
            timeout=20,
        )
        if resp.status_code == 429:
            log.warning("429 rate-limited on %s — backing off 5s", path)
            time.sleep(5)
            return None
        if resp.status_code == 403:
            log.error("403 on %s — endpoint not in your Finnhub plan", path)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("finnhub %s failed: %s", path, exc)
        return None


# ── Endpoints ────────────────────────────────────────────────────────────────
def quote(symbol: str) -> dict | None:
    return _get("/quote", {"symbol": symbol})


def profile2(symbol: str) -> dict | None:
    return _get("/stock/profile2", {"symbol": symbol})


def metric_all(symbol: str) -> dict | None:
    return _get("/stock/metric", {"symbol": symbol, "metric": "all"})


# ── Normalization helpers ────────────────────────────────────────────────────
def _pct_to_fraction(v):
    """Finnhub growth/ROE metrics are percentages (12.3 -> 0.123).

    VERIFIED 2026-06-17 against a live AAPL response: epsGrowthTTMYoy=29.01,
    revenueGrowthTTMYoy=12.76, roeTTM=146.69 — i.e. percentages, so /100 is
    correct. If a future response ever returns these as fractions, drop the /100.
    """
    return None if v is None else v / 100.0


def _fcf_growth(m: dict) -> float | None:
    """Recent FCF growth derived from Finnhub's two EV/FCF multiples.

    Both multiples share the same enterprise-value numerator, so it cancels:
        FCF_TTM / FCF_lastFY - 1 = (EV/FCF_annual) / (EV/FCF_TTM) - 1
    i.e. trailing-12-month FCF vs the last full fiscal year (a recent-FCF-growth
    measure, not strict FY-over-FY). Only meaningful when both FCF figures are
    positive, so we require both multiples > 0; otherwise return None (null=fail).
    Result is a fraction (0.31 = +31%), matching the growth-field convention.
    """
    ann = m.get("currentEv/freeCashFlowAnnual")
    ttm = m.get("currentEv/freeCashFlowTTM")
    if ann is None or ttm is None or ann <= 0 or ttm <= 0:
        return None
    return ann / ttm - 1.0


def extract_prefilter(symbol: str) -> dict | None:
    """Step 2 pre-filter inputs. quote() is the lightweight liveness probe;
    profile2() supplies country, exchange, and market cap.

    Avg daily volume is NOT available from quote/profile2 on Finnhub, so the
    volume>100K cut is deferred to the metric phase (10DayAverageTradingVolume).
    """
    q = quote(symbol)
    if not q or not q.get("c"):  # no current price -> not actively traded
        return None
    p = profile2(symbol)
    if not p:
        return None
    return {
        "ticker": symbol,
        "company": p.get("name"),
        "country": p.get("country"),
        "exchange": p.get("exchange"),
        "market_cap": p.get("marketCapitalization"),  # millions USD
        "price": q.get("c"),
        "is_nyse_or_nasdaq": is_nyse_or_nasdaq(p.get("exchange")),
    }


def extract_metrics(symbol: str) -> dict | None:
    """Step 4: pull the free metric set from /stock/metric. Returns None if the
    metric block is missing entirely."""
    payload = metric_all(symbol)
    if not payload:
        return None
    m = payload.get("metric") or {}
    if not m:
        return None

    avg_vol_millions = m.get("10DayAverageTradingVolume")
    return {
        "pe": m.get("peBasicExclExtraTTM"),
        "eps_growth": _pct_to_fraction(m.get("epsGrowthTTMYoy")),
        "revenue_growth": _pct_to_fraction(m.get("revenueGrowthTTMYoy")),
        "pb": m.get("pbAnnual"),
        "beta": m.get("beta"),
        "roe": _pct_to_fraction(m.get("roeTTM")),
        "ps": m.get("psTTM"),
        "pcf": m.get("pcfShareTTM"),
        "pfcf": m.get("pfcfShareTTM"),               # P/FCF (free, replaces premium calc)
        "debt_equity": m.get("totalDebt/totalEquityAnnual"),
        "market_cap": m.get("marketCapitalization"),  # millions USD (cross-checks profile2)
        "avg_volume": None if avg_vol_millions is None else avg_vol_millions * 1_000_000,
        "ev_ebitda": m.get("evEbitdaTTM"),             # plain ratio, exact from Finnhub
        "fcf_growth": _fcf_growth(m),                  # fraction, derived from EV/FCF multiples
    }
