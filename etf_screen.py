"""ETF Trend screen — a TECHNICAL ranking over a curated, liquid, non-leveraged ETF
universe. ETFs have no company fundamentals, so instead of PEG/ROE this ranks price
action across three lenses:

  • Momentum   — trend (above the 200-day) + relative strength vs SPY + 12-month return
  • Stability  — low annualized volatility + shallow max drawdown (calm core holdings)
  • Yield      — trailing dividend yield (income)

Curation itself is the "quality" filter: only reputable, liquid, broad / sector /
thematic / factor / bond / commodity funds — NO leveraged, inverse, or single-stock
ETFs. Data: one Yahoo daily-history call per ETF (via the proxy, no API cap) for the
price-action scores, plus one Finnhub metric call for the dividend yield.

Scores are absolute (fixed anchors, 0–100) so they're comparable and stable over
time — a first-pass heuristic to be validated by the forward-return experiment, not
gospel.
"""
from __future__ import annotations

import logging

import finnhub_client as fh
import technicals as tech

log = logging.getLogger("screener.etf")

BENCHMARK = "SPY"

# (ticker, display name, category). Curated — reputable, liquid, no leveraged/inverse/single-stock.
CURATED: list[tuple[str, str, str]] = [
    # Broad US
    ("VOO", "Vanguard S&P 500", "Broad US"), ("VTI", "Vanguard Total US Market", "Broad US"),
    ("SPLG", "SPDR Portfolio S&P 500", "Broad US"), ("QQQ", "Invesco Nasdaq-100", "Broad US"),
    ("QQQM", "Invesco Nasdaq-100 (M)", "Broad US"), ("DIA", "SPDR Dow Jones", "Broad US"),
    ("IWM", "iShares Russell 2000", "Broad US"), ("RSP", "Invesco S&P 500 Equal-Weight", "Broad US"),
    # Style
    ("VUG", "Vanguard Growth", "Style"), ("VTV", "Vanguard Value", "Style"),
    ("SCHG", "Schwab US Large-Cap Growth", "Style"), ("MGK", "Vanguard Mega-Cap Growth", "Style"),
    ("VBR", "Vanguard Small-Cap Value", "Style"), ("MTUM", "iShares Momentum Factor", "Style"),
    ("QUAL", "iShares Quality Factor", "Style"), ("USMV", "iShares Min-Volatility", "Style"),
    # Ex-US / EM
    ("VXUS", "Vanguard Total Intl", "Ex-US"), ("VEA", "Vanguard Developed Markets", "Ex-US"),
    ("IEFA", "iShares Core Developed", "Ex-US"), ("VWO", "Vanguard Emerging Markets", "Ex-US"),
    ("IEMG", "iShares Core EM", "Ex-US"), ("EWJ", "iShares MSCI Japan", "Ex-US"),
    ("INDA", "iShares MSCI India", "Ex-US"), ("MCHI", "iShares MSCI China", "Ex-US"),
    ("EWZ", "iShares MSCI Brazil", "Ex-US"), ("ISRA", "VanEck Israel", "Ex-US"),
    # Sectors
    ("XLK", "Technology Select", "Sector"), ("XLF", "Financials Select", "Sector"),
    ("XLE", "Energy Select", "Sector"), ("XLV", "Health Care Select", "Sector"),
    ("XLI", "Industrials Select", "Sector"), ("XLY", "Consumer Discretionary", "Sector"),
    ("XLP", "Consumer Staples", "Sector"), ("XLU", "Utilities Select", "Sector"),
    ("XLB", "Materials Select", "Sector"), ("XLRE", "Real Estate Select", "Sector"),
    ("XLC", "Communication Services", "Sector"), ("VGT", "Vanguard Info Tech", "Sector"),
    ("SOXX", "iShares Semiconductor", "Sector"), ("SMH", "VanEck Semiconductor", "Sector"),
    ("KRE", "SPDR Regional Banking", "Sector"), ("ITA", "iShares Aerospace & Defense", "Sector"),
    ("IBB", "iShares Biotech", "Sector"), ("XBI", "SPDR Biotech", "Sector"),
    ("XOP", "SPDR Oil & Gas E&P", "Sector"), ("VNQ", "Vanguard Real Estate", "Sector"),
    # Thematic
    ("MAGS", "Roundhill Magnificent 7", "Thematic"), ("EUAD", "Select STOXX Europe Aero/Def", "Thematic"),
    ("ARKK", "ARK Innovation", "Thematic"), ("ICLN", "iShares Clean Energy", "Thematic"),
    ("TAN", "Invesco Solar", "Thematic"), ("LIT", "Global X Lithium", "Thematic"),
    ("BOTZ", "Global X Robotics/AI", "Thematic"), ("CIBR", "First Trust Cybersecurity", "Thematic"),
    ("SKYY", "First Trust Cloud", "Thematic"), ("FDN", "First Trust Internet", "Thematic"),
    # Dividend / income
    ("SCHD", "Schwab US Dividend", "Dividend"), ("VYM", "Vanguard High Dividend Yield", "Dividend"),
    ("VIG", "Vanguard Dividend Appreciation", "Dividend"), ("DGRO", "iShares Dividend Growth", "Dividend"),
    ("NOBL", "ProShares Dividend Aristocrats", "Dividend"), ("JEPI", "JPMorgan Equity Premium Income", "Dividend"),
    ("JEPQ", "JPMorgan Nasdaq Premium Income", "Dividend"),
    # Bonds
    ("BND", "Vanguard Total Bond", "Bonds"), ("AGG", "iShares Core US Bond", "Bonds"),
    ("TLT", "iShares 20+ Year Treasury", "Bonds"), ("IEF", "iShares 7-10 Year Treasury", "Bonds"),
    ("SHY", "iShares 1-3 Year Treasury", "Bonds"), ("LQD", "iShares Investment-Grade Corp", "Bonds"),
    ("HYG", "iShares High-Yield Corp", "Bonds"), ("TIP", "iShares TIPS", "Bonds"),
    ("MUB", "iShares National Muni", "Bonds"), ("BNDX", "Vanguard Total Intl Bond", "Bonds"),
    # Commodity / gold / crypto
    ("GLD", "SPDR Gold Shares", "Commodity"), ("IAU", "iShares Gold Trust", "Commodity"),
    ("SLV", "iShares Silver Trust", "Commodity"), ("DBC", "Invesco Commodity Index", "Commodity"),
    ("IBIT", "iShares Bitcoin Trust", "Crypto"), ("ETHA", "iShares Ethereum Trust", "Crypto"),
    ("FBTC", "Fidelity Bitcoin", "Crypto"),
]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _annual_vol(closes: list[float]) -> float | None:
    """Annualized volatility from daily returns (last ~1y)."""
    window = closes[-252:]
    if len(window) < 30:
        return None
    rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window)) if window[i - 1]]
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return (var ** 0.5) * (252 ** 0.5)


def _max_drawdown(closes: list[float]) -> float | None:
    """Worst peak-to-trough decline over the last ~1y (negative fraction)."""
    window = closes[-252:]
    if len(window) < 30:
        return None
    peak, mdd = window[0], 0.0
    for c in window:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, c / peak - 1)
    return mdd


def _yield_pct(ticker: str) -> float | None:
    """Trailing/indicated dividend yield (%) from Finnhub metrics; None if absent."""
    payload = fh.metric_all(ticker)
    m = (payload or {}).get("metric") or {}
    for key in ("dividendYieldIndicatedAnnual", "currentDividendYieldTTM", "dividendYield5Y"):
        v = m.get(key)
        if isinstance(v, (int, float)):
            return round(float(v), 2)
    return None


def _momentum_score(regime: float, rs_6m: float | None, mom_12m: float | None) -> float:
    reg = _clamp01(regime)
    rs = _clamp01(((rs_6m or 0.0) + 0.05) / 0.20)   # −5% vs SPY → 0, +15% → 1
    m12 = _clamp01(((mom_12m or 0.0) + 0.10) / 0.40)  # −10% → 0, +30% → 1
    return round(100 * (0.40 * reg + 0.35 * rs + 0.25 * m12), 1)


def _stability_score(vol: float | None, mdd: float | None) -> float | None:
    if vol is None and mdd is None:
        return None
    volc = _clamp01((0.35 - (vol if vol is not None else 0.35)) / 0.25)  # 35% vol → 0, 10% → 1
    ddc = _clamp01((0.35 + (mdd if mdd is not None else -0.35)) / 0.30)  # −35% dd → 0, −5% → 1
    return round(100 * (0.60 * volc + 0.40 * ddc), 1)


def run_etf_screen() -> list[dict]:
    """Rank the curated ETF universe by momentum / stability / yield. Never raises —
    a fetch failure just drops that ETF."""
    spy_closes = tech._daily_closes(BENCHMARK)
    spy_mom_6m = tech._momentum(spy_closes, 126) if spy_closes else None

    rows: list[dict] = []
    for i, (ticker, name, category) in enumerate(CURATED, 1):
        try:
            closes = tech._daily_closes(ticker)
            if not closes or len(closes) < 60:
                continue
            price = closes[-1]
            sma_200 = tech._sma(closes, 200)
            sma_150 = tech._sma(closes, 150)
            mom_3m = tech._momentum(closes, 63)
            mom_6m = tech._momentum(closes, 126)
            mom_12m = tech._momentum(closes, 252)
            rsi_14 = tech._rsi(closes, 14)
            rs_6m = (mom_6m - spy_mom_6m) if (mom_6m is not None and spy_mom_6m is not None) else None
            vol_1y = _annual_vol(closes)
            max_dd_1y = _max_drawdown(closes)
            pct_vs_200d = (price / sma_200 - 1) if sma_200 else None
            regime = 1.0 if (pct_vs_200d is not None and pct_vs_200d >= 0) else (
                _clamp01(1 + pct_vs_200d / 0.10) if pct_vs_200d is not None else 0.0)
            rows.append({
                "ticker": ticker, "name": name, "category": category, "price": price,
                "mom_3m": mom_3m, "mom_6m": mom_6m, "mom_12m": mom_12m, "rs_6m": rs_6m,
                "pct_vs_200d": pct_vs_200d, "vol_1y": vol_1y, "max_dd_1y": max_dd_1y,
                "rsi_14": rsi_14, "ttm_yield": _yield_pct(ticker),
                "momentum_score": _momentum_score(regime, rs_6m, mom_12m),
                "stability_score": _stability_score(vol_1y, max_dd_1y),
                # Entry-quality score (reuses the same trend+pullback model; sma_150 as
                # the trend line, relative strength as the sector-tailwind input).
                "price_score": tech.price_score(price, sma_150, sma_200, mom_6m, rs_6m, rsi_14),
            })
            if i % 20 == 0:
                log.info("ETF progress %d/%d", i, len(CURATED))
        except Exception as exc:  # noqa: BLE001 - one bad ETF must not fail the screen
            log.warning("ETF %s failed: %s", ticker, exc)
    rows.sort(key=lambda r: (r.get("momentum_score") or 0), reverse=True)
    log.info("ETF screen: ranked %d/%d ETF(s)", len(rows), len(CURATED))
    return rows
