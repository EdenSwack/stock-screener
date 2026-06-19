"""End-to-end screener run: universe -> pre-filter -> metrics -> screen.

Phases are logged separately so you can see exactly how many tickers were dropped
at each stage. All metrics (incl. EV/EBITDA + FCF growth) come from the single
Finnhub /stock/metric call in the metrics phase.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import finnhub_client as fh
import screeners
from config import (
    DATA_DIR,
    RESULTS_PATH,
    SCREENERS,
    PREFILTER_MIN_MARKET_CAP_MUSD,
    PREFILTER_MIN_AVG_VOLUME,
)
from screeners import SCREENER_FILTERS, BUCKET_WEIGHTS, compute_peg, count_missing_required, score_bucket
import publish
from universe import build_universe

log = logging.getLogger("screener.pipeline")

# Output fields per the spec's JSON structure (step 8), plus the flags we add.
_OUTPUT_FIELDS = [
    "ticker", "company", "exchange", "market_cap", "price", "avg_volume",
    "peg", "eps_growth", "revenue_growth", "ev_ebitda", "fcf_growth",
    "pb", "beta", "pfcf", "roe", "ps", "pcf", "debt_equity",
]


def _prefilter(universe: list[str]) -> list[dict]:
    """PHASE 1 — cheap Finnhub quote + profile2 to drop non-US / sub-cap names.
    Volume is checked later (Finnhub exposes no volume on quote/profile2)."""
    survivors, dropped_data, dropped_country, dropped_mcap = [], 0, 0, 0
    for i, ticker in enumerate(universe, 1):
        if i % 100 == 0:
            log.info("pre-filter progress %d/%d", i, len(universe))
        base = fh.extract_prefilter(ticker)
        if base is None:
            dropped_data += 1
            continue
        if base.get("country") != "US":
            dropped_country += 1
            continue
        mcap = base.get("market_cap")
        if mcap is None or mcap < PREFILTER_MIN_MARKET_CAP_MUSD:
            dropped_mcap += 1
            continue
        survivors.append(base)
    log.info(
        "PHASE 1 pre-filter: universe=%d survived=%d | dropped: no_data=%d non_US=%d mcap=%d",
        len(universe), len(survivors), dropped_data, dropped_country, dropped_mcap,
    )
    return survivors


def _metrics(survivors: list[dict]) -> list[dict]:
    """PHASE 2 — Finnhub /stock/metric pull, deferred volume cut, missing-data skip."""
    out, dropped_no_metrics, dropped_volume, dropped_missing = [], 0, 0, 0
    for base in survivors:
        m = fh.extract_metrics(base["ticker"])
        if m is None:
            dropped_no_metrics += 1
            continue
        stock = {**base, **m, "nyse_or_nasdaq": base.get("is_nyse_or_nasdaq")}

        vol = stock.get("avg_volume")
        if vol is not None and vol < PREFILTER_MIN_AVG_VOLUME:
            dropped_volume += 1
            continue
        if count_missing_required(stock) > 3:
            dropped_missing += 1
            log.info("skip %s — >3 required metric fields missing", stock["ticker"])
            continue

        stock["peg"] = compute_peg(stock.get("pe"), stock.get("eps_growth"))
        out.append(stock)
    log.info(
        "PHASE 2 metrics: in=%d survived=%d | dropped: no_metrics=%d low_volume=%d missing_fields=%d",
        len(survivors), len(out), dropped_no_metrics, dropped_volume, dropped_missing,
    )
    return out


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
    survivors = _prefilter(universe)
    stocks = _metrics(survivors)
    buckets = _apply_screeners(stocks)

    results = {
        "last_updated": started.isoformat(),
        "stats": {
            "universe": len(universe),
            "prefilter_survivors": len(survivors),
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
