"""Map Finnhub's granular industry → a GICS sector + its SPDR sector ETF, and
compute 3-month sector relative strength (sector ETF return − SPY return) from
the price proxy. Used to tag each qualifier with its sector and how that sector
is trending vs the market. Any failure yields None (never breaks the run)."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("screener.sectors")

PROXY = "https://finhub-ticker-proxy.edenswack1.workers.dev"

# GICS sector → SPDR sector ETF (REITs are excluded from the universe, but XLRE
# is kept here in case an industry still maps to Real Estate).
SECTOR_ETF = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}

# Substring → sector, checked in order (specific before generic). Finnhub's
# finnhubIndustry strings are granular, so keyword-match rather than exact-map.
_KEYWORDS: list[tuple[str, str]] = [
    ("semiconduct", "Technology"), ("software", "Technology"), ("hardware", "Technology"),
    ("electronic", "Technology"), ("information technology", "Technology"), ("technology", "Technology"),
    ("telecom", "Communication Services"), ("media", "Communication Services"),
    ("entertainment", "Communication Services"), ("interactive", "Communication Services"),
    ("communication", "Communication Services"),
    ("bank", "Financials"), ("insurance", "Financials"), ("capital market", "Financials"),
    ("financial", "Financials"), ("diversified financ", "Financials"),
    ("pharmaceutic", "Health Care"), ("biotech", "Health Care"), ("life science", "Health Care"),
    ("medical", "Health Care"), ("health", "Health Care"),
    ("food", "Consumer Staples"), ("beverage", "Consumer Staples"), ("tobacco", "Consumer Staples"),
    ("household", "Consumer Staples"), ("personal product", "Consumer Staples"), ("staples", "Consumer Staples"),
    ("retail", "Consumer Discretionary"), ("automobile", "Consumer Discretionary"), ("auto ", "Consumer Discretionary"),
    ("hotel", "Consumer Discretionary"), ("restaurant", "Consumer Discretionary"), ("leisure", "Consumer Discretionary"),
    ("apparel", "Consumer Discretionary"), ("luxury", "Consumer Discretionary"), ("discretionary", "Consumer Discretionary"),
    ("oil", "Energy"), ("gas", "Energy"), ("coal", "Energy"), ("energy", "Energy"),
    ("aerospace", "Industrials"), ("defense", "Industrials"), ("machinery", "Industrials"),
    ("airline", "Industrials"), ("transportation", "Industrials"), ("logistics", "Industrials"),
    ("railroad", "Industrials"), ("construction", "Industrials"), ("building", "Industrials"),
    ("engineering", "Industrials"), ("industrial", "Industrials"),
    ("chemical", "Materials"), ("metal", "Materials"), ("mining", "Materials"),
    ("steel", "Materials"), ("paper", "Materials"), ("material", "Materials"),
    ("water utilit", "Utilities"), ("electric utilit", "Utilities"), ("utilit", "Utilities"),
    ("real estate", "Real Estate"),
]


def industry_to_sector(industry: str | None) -> str | None:
    if not industry:
        return None
    low = industry.lower()
    for kw, sector in _KEYWORDS:
        if kw in low:
            return sector
    return None


def _three_month_return(symbol: str) -> float | None:
    try:
        r = requests.get(f"{PROXY}/", params={"symbol": symbol, "range": "3mo", "interval": "1d"}, timeout=30)
        if not r.ok:
            return None
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        closes = [c for c in ((res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
        if len(closes) < 2 or closes[0] == 0:
            return None
        return closes[-1] / closes[0] - 1
    except Exception as exc:  # noqa: BLE001
        log.warning("3m return fetch failed for %s: %s", symbol, exc)
        return None


def sector_relative_strength() -> dict[str, float]:
    """{sector: 3-month return − SPY 3-month return}. Empty on SPY failure."""
    spy = _three_month_return("SPY")
    if spy is None:
        log.warning("SPY 3m return unavailable — skipping sector strength")
        return {}
    out: dict[str, float] = {}
    for sector, etf in SECTOR_ETF.items():
        r = _three_month_return(etf)
        if r is not None:
            out[sector] = round(r - spy, 4)
    log.info("sector relative strength computed for %d sector(s)", len(out))
    return out
