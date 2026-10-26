"""Forward (consensus) earnings estimates via yfinance, for the forward PEG and the
next-year-growth filter.

Trailing YoY EPS growth (Finnhub epsGrowthTTMYoy) is dominated by base effects —
rebounds from a loss year, one-off tax benefits, merger accounting — which drive the
trailing PEG to ~0 and let one-off earners through. Consensus next-fiscal-year EPS
(analysts' adjusted basis, so P/E and growth are on the same basis) looks forward
instead. Any failure / missing coverage yields None (null=fail in the screens).
"""

from __future__ import annotations

import logging
import time

import yfinance as yf

log = logging.getLogger("screener.estimates")

_THROTTLE_S = 0.4  # be gentle with Yahoo; ~2 calls per ticker


def fetch_forward(symbol: str) -> dict | None:
    """Return {eps_fwd, eps_growth_fwd, pe_fwd} for the NEXT fiscal year ('+1y'):
    consensus EPS, its growth vs the current fiscal year's consensus (fraction), and
    price / that EPS. None if Yahoo has no estimate or no price."""
    try:
        t = yf.Ticker(symbol.replace(".", "-"))  # BRK.B -> BRK-B (Yahoo convention)
        est = t.earnings_estimate
        price = t.fast_info.get("lastPrice")
    except Exception as exc:  # noqa: BLE001
        log.debug("estimates %s failed: %s", symbol, exc)
        return None
    finally:
        time.sleep(_THROTTLE_S)
    if est is None or est.empty or "+1y" not in est.index:
        return None
    row = est.loc["+1y"]
    eps, growth = row.get("avg"), row.get("growth")
    if eps is None or growth is None or eps != eps or growth != growth:  # NaN check
        return None
    pe_fwd = float(price / eps) if price and eps > 0 else None
    return {"eps_fwd": float(eps), "eps_growth_fwd": float(growth), "pe_fwd": pe_fwd}
