"""Twelve Data client — ATR only, for trailing/chandelier stops.

Free tier is 8 requests/min, so calls are throttled. ATR is fetched only for the
small set of qualifying tickers (not the whole universe), so the daily 800-call
cap is never an issue. Any failure yields None (the frontend falls back to a %
stop), never an exception.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from config import (
    TWELVE_DATA_API_KEY,
    TWELVE_DATA_BASE,
    TWELVE_DATA_RATE_LIMIT_PER_MIN,
    ATR_PERIOD,
)

log = logging.getLogger("screener.twelvedata")

_MIN_INTERVAL = 60.0 / TWELVE_DATA_RATE_LIMIT_PER_MIN
_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def fetch_atr(symbol: str) -> float | None:
    """Latest 14-day ATR for the symbol, or None on any failure / missing key."""
    if not TWELVE_DATA_API_KEY:
        return None
    _throttle()
    try:
        resp = requests.get(
            f"{TWELVE_DATA_BASE}/atr",
            params={"symbol": symbol, "interval": "1day", "time_period": ATR_PERIOD, "apikey": TWELVE_DATA_API_KEY},
            timeout=20,
        )
        data = resp.json()
        if data.get("status") != "ok":
            log.warning("ATR %s -> %s", symbol, data.get("message") or data.get("status"))
            return None
        values = data.get("values") or []
        if not values:
            return None
        return float(values[0]["atr"])
    except Exception as exc:  # noqa: BLE001 - never let an ATR failure break the run
        log.warning("ATR fetch failed for %s: %s", symbol, exc)
        return None
