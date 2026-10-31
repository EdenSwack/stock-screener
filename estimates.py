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
from datetime import date

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


def _cagr(series) -> float | None:
    """Annual growth rate from a newest-first annual series, over up to 3 years.
    Both ends must be positive (growth off a loss year is the base effect we're
    avoiding); fewer than 3 annual values -> None."""
    vals = [float(v) for v in series if v == v]  # drop NaN, keep newest-first order
    vals = vals[:4]
    if len(vals) < 3 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    return (vals[0] / vals[-1]) ** (1 / (len(vals) - 1)) - 1


def fetch_fundamentals(symbol: str) -> dict:
    """Multi-year growth, cash conversion, leverage and the next earnings date:
    {eps_growth_3y, revenue_growth_3y (annual CAGR, fractions), fcf_margin (TTM FCF /
    revenue), net_debt_ebitda ((debt - cash) / EBITDA; None if EBITDA <= 0),
    next_earnings (YYYY-MM-DD)}. Each field is None when Yahoo lacks it."""
    out = {"eps_growth_3y": None, "revenue_growth_3y": None, "fcf_margin": None,
           "net_debt_ebitda": None, "next_earnings": None}
    t = yf.Ticker(symbol.replace(".", "-"))
    try:
        inc = t.income_stmt
        if "Diluted EPS" in inc.index:
            out["eps_growth_3y"] = _cagr(inc.loc["Diluted EPS"])
        if "Total Revenue" in inc.index:
            out["revenue_growth_3y"] = _cagr(inc.loc["Total Revenue"])
    except Exception as exc:  # noqa: BLE001
        log.debug("income_stmt %s failed: %s", symbol, exc)
    try:
        info = t.info
        fcf, rev = info.get("freeCashflow"), info.get("totalRevenue")
        if fcf is not None and rev:
            out["fcf_margin"] = fcf / rev
        debt, cash, ebitda = info.get("totalDebt"), info.get("totalCash"), info.get("ebitda")
        if ebitda and ebitda > 0:
            out["net_debt_ebitda"] = ((debt or 0) - (cash or 0)) / ebitda
    except Exception as exc:  # noqa: BLE001
        log.debug("info %s failed: %s", symbol, exc)
    try:
        dates = (t.calendar or {}).get("Earnings Date") or []
        upcoming = sorted(d for d in dates if isinstance(d, date) and d >= date.today())
        out["next_earnings"] = upcoming[0].isoformat() if upcoming else None
    except Exception as exc:  # noqa: BLE001
        log.debug("calendar %s failed: %s", symbol, exc)
    time.sleep(_THROTTLE_S)
    return out
