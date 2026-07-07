"""Price-action technicals + an absolute Price Score (0-100) for qualifiers.

From a single daily-price fetch per ticker (Yahoo proxy — no API cap) we compute
SMA-150/200, RSI-14, Bollinger %B, and 3/6-month momentum. The Price Score uses a
"trend + pullback entry" philosophy: reward confirmed uptrends (above the 200-day,
positive 6-month momentum, leading sector) that are NOT overextended (price near/
just-below the EMA-150, not stretched far above). RSI and Bollinger are returned
as context flags, NOT folded into the score (they're non-monotonic).
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("screener.technicals")

PROXY = "https://finhub-ticker-proxy.edenswack1.workers.dev"


def _daily_closes(symbol: str) -> list[float] | None:
    try:
        r = requests.get(f"{PROXY}/", params={"symbol": symbol, "range": "2y", "interval": "1d"}, timeout=30)
        if not r.ok:
            return None
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        closes = [c for c in ((res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
        return closes or None
    except Exception as exc:  # noqa: BLE001
        log.warning("daily closes fetch failed for %s: %s", symbol, exc)
        return None


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain, avg_loss = gains / n, losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _bollinger_pct(closes: list[float], n: int = 20, k: float = 2.0) -> float | None:
    """%B = (price − lower) / (upper − lower). <0 below lower band, >1 above upper."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mean = sum(window) / n
    var = sum((c - mean) ** 2 for c in window) / n
    sd = var ** 0.5
    if sd == 0:
        return 0.5
    upper, lower = mean + k * sd, mean - k * sd
    return round((closes[-1] - lower) / (upper - lower), 3)


def _momentum(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-1 - bars] == 0:
        return None
    return closes[-1] / closes[-1 - bars] - 1


def fetch_technicals(symbol: str) -> dict | None:
    """Indicators from one daily-price fetch, or None if unavailable."""
    closes = _daily_closes(symbol)
    if not closes or len(closes) < 30:
        return None
    return {
        "sma_150": _sma(closes, 150),
        "sma_200": _sma(closes, 200),
        "rsi_14": _rsi(closes, 14),
        "bb_pct": _bollinger_pct(closes, 20, 2.0),
        "mom_1w": _momentum(closes, 5),
        "mom_3m": _momentum(closes, 63),
        "mom_6m": _momentum(closes, 126),
    }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# Sub-score weights (trend + pullback entry). Proximity (pullback, not stretched)
# is weighted above momentum so the score rewards a sane entry, not just strength.
# Null components drop out and their weight is redistributed (like score_bucket).
_PRICE_WEIGHTS = {"regime": 0.30, "proximity": 0.35, "momentum": 0.20, "sector": 0.15}


def price_score(price, ema_150, sma_200, mom_6m, sector_rs_3m, rsi_14=None) -> float | None:
    """Absolute 0-100 "how attractive is the entry price" score (trend + pullback).
    A final extension penalty haircuts chasing an overbought / far-above-trend
    name, so a stretched chart can't score well on momentum alone."""
    comps: dict[str, float] = {}

    # Regime: above the 200-day = uptrend (1.0); fades to 0 by ~10% below it.
    if price and sma_200:
        ratio = price / sma_200 - 1
        comps["regime"] = 1.0 if ratio >= 0 else _clamp01(1 + ratio / 0.10)

    # Proximity: best at/just-below the EMA-150 (pullback in an uptrend); penalize
    # stretched-far-above and broken-far-below.
    if price and ema_150:
        pct = price / ema_150 - 1
        if -0.05 <= pct <= 0.05:
            comps["proximity"] = 1.0
        elif -0.15 <= pct < -0.05:
            comps["proximity"] = 0.5 + 0.5 * (pct + 0.15) / 0.10  # -0.15→0.5 … -0.05→1.0
        elif pct < -0.15:
            comps["proximity"] = 0.1
        elif 0.05 < pct <= 0.20:
            comps["proximity"] = 1.0 - 0.7 * (pct - 0.05) / 0.15  # +0.05→1.0 … +0.20→0.3
        else:
            comps["proximity"] = 0.1

    # Momentum: 6-month return; 0%→0.4, +30%→1.0, −20%→0.
    if mom_6m is not None:
        comps["momentum"] = _clamp01((mom_6m + 0.2) / 0.5)

    # Sector tailwind: 3m relative strength; 0→0.5, +5%→1, −5%→0.
    if sector_rs_3m is not None:
        comps["sector"] = _clamp01((sector_rs_3m + 0.05) / 0.10)

    total_w = sum(_PRICE_WEIGHTS[k] for k in comps)
    if not total_w:
        return None
    base = 100 * sum(_PRICE_WEIGHTS[k] * v for k, v in comps.items()) / total_w

    # Extension penalty: chasing an overbought (RSI > 70) or far-above-trend
    # (>20% over EMA-150) name is a poor entry — haircut up to 50%.
    ext = 0.0
    if price and ema_150:
        over = price / ema_150 - 1
        if over > 0.20:
            ext = max(ext, min(1.0, (over - 0.20) / 0.20))  # +20%→0 … +40%→1
    if rsi_14 is not None and rsi_14 > 70:
        ext = max(ext, min(1.0, (rsi_14 - 70) / 30))  # 70→0 … 100→1
    return round(base * (1 - 0.5 * ext), 1)
