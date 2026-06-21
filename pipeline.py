"""End-to-end screener run: universe -> metrics -> screen -> prices.

The universe is Finnhub's symbol list filtered to NYSE/NASDAQ common stock (REITs
excluded by type). Each ticker then takes ONE /stock/metric call (market cap + all
screening metrics incl. EV/EBITDA + FCF growth); price is fetched only for the
qualifiers. Phases are logged so you can see how many tickers drop at each stage.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import finnhub_client as fh
import screeners
import twelvedata as td
from config import (
    DATA_DIR,
    RESULTS_PATH,
    SCREENERS,
    PREFILTER_MIN_MARKET_CAP_MUSD,
    PREFILTER_MIN_AVG_VOLUME,
    TWELVE_DATA_API_KEY,
)
from screeners import SCREENER_FILTERS, BUCKET_WEIGHTS, compute_peg, count_missing_required, score_bucket
import publish
from universe import build_universe

log = logging.getLogger("screener.pipeline")

# Output fields per the spec's JSON structure (step 8), plus the flags we add.
_OUTPUT_FIELDS = [
    "ticker", "company", "exchange", "market_cap", "price", "avg_volume",
    "peg", "eps_growth", "revenue_growth", "ev_ebitda", "fcf_growth",
    "pb", "beta", "pfcf", "roe", "ps", "pcf", "debt_equity", "atr", "ema_150",
]


def _metrics(universe: list[dict]) -> list[dict]:
    """PHASE 1 — one Finnhub /stock/metric call per ticker (the only per-ticker
    call now). Applies the market-cap floor, volume cut, and missing-data skip,
    and computes PEG. The universe is already exchange/type-filtered (NYSE/NASDAQ
    common, REITs excluded), so no profile2 is needed."""
    out = []
    dropped_no_metrics = dropped_mcap = dropped_volume = dropped_missing = 0
    total = len(universe)
    for i, base in enumerate(universe, 1):
        if i % 250 == 0:
            log.info("metrics progress %d/%d (kept %d)", i, total, len(out))
        m = fh.extract_metrics(base["ticker"])
        if m is None:
            dropped_no_metrics += 1
            continue
        stock = {**base, **m, "nyse_or_nasdaq": True}

        mcap = stock.get("market_cap")
        if mcap is None or mcap < PREFILTER_MIN_MARKET_CAP_MUSD:
            dropped_mcap += 1
            continue
        vol = stock.get("avg_volume")
        if vol is not None and vol < PREFILTER_MIN_AVG_VOLUME:
            dropped_volume += 1
            continue
        if count_missing_required(stock) > 3:
            dropped_missing += 1
            continue

        stock["peg"] = compute_peg(stock.get("pe"), stock.get("eps_growth"))
        out.append(stock)
    log.info(
        "PHASE 1 metrics: universe=%d kept=%d | dropped: no_metrics=%d mcap=%d low_volume=%d missing=%d",
        total, len(out), dropped_no_metrics, dropped_mcap, dropped_volume, dropped_missing,
    )
    return out


def _fill_prices(buckets: dict[str, list[dict]]) -> None:
    """PHASE 3 — fetch the live price for QUALIFYING tickers only (one quote each)
    and patch it into every bucket record. Price isn't a screening factor, so only
    the handful that qualify need it."""
    tickers = {r["ticker"] for rows in buckets.values() for r in rows}
    prices: dict[str, float | None] = {}
    for t in tickers:
        q = fh.quote(t)
        prices[t] = q.get("c") if q else None
    for rows in buckets.values():
        for r in rows:
            r["price"] = prices.get(r["ticker"])
    log.info("PHASE 3 prices: fetched for %d qualifying ticker(s)", len(tickers))


def _fill_atr(buckets: dict[str, list[dict]]) -> None:
    """PHASE 4 — fetch 14-day ATR (Twelve Data) for QUALIFYING tickers only, for
    volatility-adjusted/chandelier stops in the frontend. Throttled to the free
    tier; no key -> skipped (frontend falls back to a % stop)."""
    tickers = {r["ticker"] for rows in buckets.values() for r in rows}
    if not TWELVE_DATA_API_KEY:
        log.info("TWELVE_DATA_API_KEY not set — skipping ATR phase")
        return
    atrs: dict[str, float | None] = {}
    for i, t in enumerate(sorted(tickers), 1):
        atrs[t] = td.fetch_atr(t)
        if i % 10 == 0:
            log.info("ATR progress %d/%d", i, len(tickers))
    got = sum(1 for v in atrs.values() if v is not None)
    for rows in buckets.values():
        for r in rows:
            r["atr"] = atrs.get(r["ticker"])
    log.info("PHASE 4 ATR: got %d/%d ticker(s)", got, len(tickers))


def _fill_ema(buckets: dict[str, list[dict]]) -> None:
    """PHASE 5 — fetch the EMA-150 (Twelve Data) for QUALIFYING tickers only. The
    frontend shows % distance of price from this line as a trend indicator (display
    only, not a filter). Throttled to the free tier; no key -> skipped."""
    tickers = {r["ticker"] for rows in buckets.values() for r in rows}
    if not TWELVE_DATA_API_KEY:
        log.info("TWELVE_DATA_API_KEY not set — skipping EMA phase")
        return
    emas: dict[str, float | None] = {}
    for i, t in enumerate(sorted(tickers), 1):
        emas[t] = td.fetch_ema(t)
        if i % 10 == 0:
            log.info("EMA progress %d/%d", i, len(tickers))
    got = sum(1 for v in emas.values() if v is not None)
    for rows in buckets.values():
        for r in rows:
            r["ema_150"] = emas.get(r["ticker"])
    log.info("PHASE 5 EMA-150: got %d/%d ticker(s)", got, len(tickers))


def _apply_screeners(stocks: list[dict]) -> dict[str, list[dict]]:
    """PHASE 4 — tag each stock with the screeners it qualifies for and bucket it."""
    buckets: dict[str, list[dict]] = {k: [] for k in SCREENER_FILTERS}
    for stock in stocks:
        qualified, any_partial = [], False
        for key in SCREENER_FILTERS:
            ok, partial = screeners.evaluate(stock, key)
            if ok:
                qualified.append(key)
                any_partial = any_partial or partial
        if not qualified:
            continue
        record = {f: stock.get(f) for f in _OUTPUT_FIELDS}
        record["screeners"] = qualified
        record["partial_data"] = any_partial
        record["quality_score"] = screeners.quality_score(stock)  # absolute, cross-screener
        # One independent copy per bucket — scoring is per-screener (min-max within
        # the set), so a shared object would let the last bucket overwrite the rest.
        for key in qualified:
            buckets[key].append(dict(record))

    for key, rows in buckets.items():
        score_bucket(rows, BUCKET_WEIGHTS[key])
        rows.sort(key=lambda r: r["composite_score"], reverse=True)
        log.info("PHASE 4 screener %-22s -> %d matches", key, len(rows))
    return buckets


def run() -> dict:
    """Run the full pipeline, write results.json, and return the results dict."""
    started = datetime.now(timezone.utc)
    log.info("=== screener run started %s ===", started.isoformat())

    universe = build_universe()
    stocks = _metrics(universe)
    buckets = _apply_screeners(stocks)
    _fill_prices(buckets)
    _fill_atr(buckets)
    _fill_ema(buckets)

    results = {
        "last_updated": started.isoformat(),
        "stats": {
            "universe": len(universe),
            "metric_survivors": len(stocks),
            "counts": {k: len(v) for k, v in buckets.items()},
        },
        "labels": SCREENERS,
        "screeners": buckets,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2))
    os.replace(tmp, RESULTS_PATH)  # atomic — readers never see a half-written file

    # Publish to Supabase so the frontend reads it from there (not this service).
    # No-ops with a log line if SUPABASE_* env vars aren't set.
    publish.to_supabase(results)
    publish.history_to_supabase(results)  # append this run's membership for history/forward-returns

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("=== screener run finished in %.0fs ===", elapsed)
    return results


def load_results() -> dict | None:
    if not RESULTS_PATH.exists():
        return None
    return json.loads(RESULTS_PATH.read_text())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
