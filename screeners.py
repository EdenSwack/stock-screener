"""Screener filter logic, PEG computation, the missing-data rule, and scoring.

Key behavior (null=fail): every filter field must be present AND in range. A null
on any filtered field causes the stock to fail that screener. This matches
TradingView, where a null metric fails a range filter. All fields (including
EV/EBITDA and FCF growth) come from the single Finnhub /stock/metric call, so a
null reflects genuinely-missing data, not a flaky second source.
"""

from __future__ import annotations

# null=fail for every field (TradingView parity). Kept as a toggle: add
# "ev_ebitda" / "fcf_growth" back here to restore the lenient / partial_data
# behavior (a null lenient field is then skipped rather than failing the screener).
LENIENT_FIELDS: set[str] = set()

# Core Finnhub metric fields used by the >3-missing skip rule (step 7). EV/EBITDA
# and FCF growth are intentionally excluded — they're allowed to be individually
# null (and then fail their own range filter) without tripping the skip.
REQUIRED_METRIC_FIELDS = [
    "pe", "eps_growth", "revenue_growth", "pb", "beta", "roe",
    "ps", "pcf", "pfcf", "debt_equity", "market_cap", "avg_volume",
]

# Each screener is a list of (field, predicate, label).
SCREENER_FILTERS = {
    "growth_tech": [
        ("peg", lambda v: 0.5 <= v <= 1.0, "PEG 0.5–1"),
        ("eps_growth", lambda v: v > 0.06, "EPS growth > 6%"),
        ("revenue_growth", lambda v: v > 0.08, "Revenue growth > 8%"),
        ("ev_ebitda", lambda v: 20 <= v <= 31, "EV/EBITDA 20–31"),
        ("fcf_growth", lambda v: v > 0.0, "FCF growth > 0%"),
        ("nyse_or_nasdaq", lambda v: v is True, "NYSE/NASDAQ"),
    ],
    "growth_tech_refined": [
        ("peg", lambda v: 0.5 <= v <= 1.0, "PEG 0.5–1"),
        ("eps_growth", lambda v: v > 0.06, "EPS growth > 6%"),
        ("revenue_growth", lambda v: v > 0.08, "Revenue growth > 8%"),
        ("ev_ebitda", lambda v: 20 <= v <= 31, "EV/EBITDA 20–31"),
        ("fcf_growth", lambda v: v > 0.0, "FCF growth > 0%"),
        ("pb", lambda v: v < 9, "P/B < 9"),
        ("beta", lambda v: 0 <= v <= 1.5, "Beta 0–1.5"),
        ("pfcf", lambda v: 0 <= v <= 29, "P/FCF 0–29"),
        ("nyse_or_nasdaq", lambda v: v is True, "NYSE/NASDAQ"),
    ],
    "traditional_value": [
        ("market_cap", lambda v: v > 300, "Market cap > $300M"),
        ("peg", lambda v: v < 1.0, "PEG < 1"),
        ("eps_growth", lambda v: v > 0.06, "EPS growth > 6%"),
        ("revenue_growth", lambda v: v > 0.08, "Revenue growth > 8%"),
        ("pb", lambda v: v < 1.0, "P/B < 1"),
        ("ev_ebitda", lambda v: v < 9, "EV/EBITDA < 9"),
        ("roe", lambda v: v > 0.14, "ROE > 14%"),
        ("ps", lambda v: v < 2.0, "P/S < 2"),
        ("pcf", lambda v: v < 15, "P/CF < 15"),
        ("debt_equity", lambda v: v < 1.5, "Debt/Equity < 1.5"),
        ("beta", lambda v: 0.5 <= v <= 1.4, "Beta 0.5–1.4"),
        ("pfcf", lambda v: 0 <= v <= 15, "P/FCF 0–15"),
        ("nyse_or_nasdaq", lambda v: v is True, "NYSE/NASDAQ"),
    ],
    "momentum_breakout": [
        ("revenue_growth", lambda v: v > 0.20, "Revenue growth > 20%"),
        ("eps_growth", lambda v: v > 0.15, "EPS growth > 15%"),
        ("beta", lambda v: 1.0 <= v <= 2.0, "Beta 1.0–2.0"),
        ("market_cap", lambda v: v > 500, "Market cap > $500M"),
        ("nyse_or_nasdaq", lambda v: v is True, "NYSE/NASDAQ"),
    ],
}


def compute_peg(pe, eps_growth) -> float | None:
    """PEG = P/E / (EPS-growth-fraction × 100). Only when both are positive
    (spec step 5). eps_growth is stored as a fraction, so ×100 yields growth %."""
    if pe is None or eps_growth is None or pe <= 0 or eps_growth <= 0:
        return None
    return pe / (eps_growth * 100.0)


def count_missing_required(stock: dict) -> int:
    return sum(1 for f in REQUIRED_METRIC_FIELDS if stock.get(f) is None)


def evaluate(stock: dict, screener_key: str):
    """Return (qualifies: bool, partial: bool).

    qualifies -> stock satisfies every evaluable predicate.
    partial   -> at least one lenient (EV/EBITDA, FCF growth) predicate was
                 skipped because the value was null.
    """
    partial = False
    for field, check, _label in SCREENER_FILTERS[screener_key]:
        val = stock.get(field)
        if val is None:
            if field in LENIENT_FIELDS:
                partial = True  # not evaluated, keep going
                continue
            return False, False  # non-lenient null -> fails this screener
        if not check(val):
            return False, partial
    return True, partial


# ── Composite scoring ────────────────────────────────────────────────────────
# Two weight buckets. Screeners 2+3 use the richer 6-factor blend; screeners 1+4
# use the 4-factor blend. Normalization is min-max WITHIN each screener's result
# set (so a score is only meaningful relative to that screener's matches).
WEIGHTS_70 = {  # growth_tech_refined, traditional_value
    "revenue_growth": 0.25,
    "eps_growth": 0.20,
    "peg": 0.20,
    "fcf_growth": 0.15,
    "roe": 0.10,
    "beta": 0.10,
}
WEIGHTS_30 = {  # growth_tech, momentum_breakout
    "revenue_growth": 0.30,
    "eps_growth": 0.25,
    "peg": 0.20,
    "fcf_growth": 0.25,
}
# Lower raw value scores higher (1 - normalized).
INVERTED_FACTORS = {"peg", "beta"}

BUCKET_WEIGHTS = {
    "growth_tech": WEIGHTS_30,
    "momentum_breakout": WEIGHTS_30,
    "growth_tech_refined": WEIGHTS_70,
    "traditional_value": WEIGHTS_70,
}


def score_bucket(rows: list[dict], weights: dict[str, float]) -> None:
    """Assign each row a 0–100 ``composite_score`` in place.

    Normalization is min-max across the rows in this bucket. Any null factor
    (e.g. FCF growth when Finnhub lacks the EV/FCF multiples, or PEG for a momentum
    name with no positive earnings) is dropped and its weight redistributed proportionally
    among the present factors — i.e. the score is the weighted mean over only the
    factors that have a value. The spec calls this out for FCF; we apply the same
    proportional rule to any null factor so a missing value never poisons a score.
    """
    ranges: dict[str, tuple[float, float]] = {}
    for f in weights:
        vals = [r[f] for r in rows if r.get(f) is not None]
        if vals:
            ranges[f] = (min(vals), max(vals))

    for r in rows:
        acc, total_w = 0.0, 0.0
        for f, w in weights.items():
            v = r.get(f)
            if v is None or f not in ranges:
                continue  # weight redistributed by exclusion from total_w
            lo, hi = ranges[f]
            norm = 0.5 if hi == lo else (v - lo) / (hi - lo)
            if f in INVERTED_FACTORS:
                norm = 1.0 - norm
            acc += w * norm
            total_w += w
        r["composite_score"] = round(100 * acc / total_w, 1) if total_w else 0.0
