"""Central configuration for the stock screener.

All infrastructure constants live here. Screener filter thresholds live next to
the filter logic in ``screeners.py`` so the business rules read in one place.

Units convention used across the pipeline (keep this straight — it is the most
common source of bugs):
  * market_cap        -> millions of USD   (matches Finnhub ``marketCapitalization``)
  * avg_volume        -> shares            (Finnhub ``10DayAverageTradingVolume`` is in
                                            millions of shares, converted on ingest)
  * growth / roe      -> fraction          (Finnhub returns these as percent; we /100)
  * pe, pb, ps, pcf, pfcf, ev_ebitda, peg, debt_equity, beta -> plain ratios
  * ev_ebitda comes from Finnhub ``evEbitdaTTM``; fcf_growth is derived from
    Finnhub's EV/FCF multiples (fraction) — see finnhub_client._fcf_growth
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Finnhub ──────────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
FINNHUB_BASE = "https://finnhub.io/api/v1"

# Stay under the free-tier 60 calls/min ceiling.
FINNHUB_RATE_LIMIT_PER_MIN = 55

# ── Universe sources (free static lists) ─────────────────────────────────────
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
NASDAQ100_CSV = "https://raw.githubusercontent.com/datasets/nasdaq-100/master/data/constituents.csv"
# iShares Russell 2000 ETF (IWM) holdings export.
RUSSELL2000_CSV = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

# ── Pre-filter thresholds (NOT screener filters — purely to cut API calls) ────
PREFILTER_MIN_MARKET_CAP_MUSD = 200.0   # $200M, expressed in millions
PREFILTER_MIN_AVG_VOLUME = 100_000      # shares/day

# ── Scheduling ───────────────────────────────────────────────────────────────
# 23:00 Israel time. Using the named tz (not a fixed UTC hour) so the run stays
# at 23:00 local across daylight-saving changes — a fixed 20:00 UTC would drift
# to 22:00 local in winter when Israel is UTC+2.
SCHEDULE_TZ = "Asia/Jerusalem"
SCHEDULE_HOUR = 23
SCHEDULE_MINUTE = 0

# ── Output ───────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"
RESULTS_PATH = DATA_DIR / "results.json"

# Screener registry: stable keys -> human label (frontend tab order follows this).
SCREENERS = {
    "growth_tech": "Growth / Tech",
    "growth_tech_refined": "Growth / Tech Refined",
    "traditional_value": "Traditional / Asset-Heavy",
    "momentum_breakout": "Momentum / Breakout",
}


def is_nyse_or_nasdaq(exchange: str | None) -> bool:
    """Finnhub profile2 returns verbose exchange names, e.g.
    'NEW YORK STOCK EXCHANGE, INC.' or 'NASDAQ NMS - GLOBAL MARKET'."""
    if not exchange:
        return False
    e = exchange.upper()
    return "NASDAQ" in e or "NEW YORK STOCK EXCHANGE" in e or "NYSE" in e
