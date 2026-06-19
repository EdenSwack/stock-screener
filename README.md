# Stock Screener (Finnhub)

Builds a US-stock universe, pre-filters it, pulls fundamentals from Finnhub,
applies four screeners nightly, and publishes the results to Supabase (the
frontend reads from there). A small HTTP API is also available for local/manual use.

## Why this is a Python service (not a Cloudflare Worker)

The full run is a long, rate-limited batch: the universe is a few thousand
tickers and Finnhub's free tier caps at ~55 calls/min, so a run takes tens of
minutes — well past a Worker's CPU/wall-clock limits. It runs as a **GitHub
Actions cron** (`.github/workflows/screener.yml`) that executes `pipeline.py` and
publishes to Supabase. Keep the split: frontend on Cloudflare Pages, proxies on
Workers, this batch on GitHub Actions, data in Supabase.

## Data sources

Every field comes from Finnhub free-tier endpoints — there is **no second data
source and no per-ticker enrichment step**.

| Field group | Source | Endpoint |
|---|---|---|
| Universe | GitHub CSVs + iShares IWM | static files |
| Country, exchange, market cap, price | Finnhub (free) | `/quote`, `/stock/profile2` |
| P/E, EPS growth, revenue growth, P/B, beta, ROE, P/S, P/CF, P/FCF, D/E, volume | Finnhub (free) | `/stock/metric?metric=all` |
| EV/EBITDA | Finnhub (free) | `evEbitdaTTM` from `/stock/metric` |
| FCF growth | Finnhub (free), derived | `currentEv/freeCashFlowAnnual ÷ ...TTM − 1` |

**FCF growth basis:** the two EV/FCF multiples share the same enterprise-value
numerator, so it cancels and the ratio is `FCF_TTM / FCF_lastFY − 1` — trailing-12-
month FCF vs the last full fiscal year (a recent-FCF-growth measure, not strict
FY-over-FY). Computed only when both FCF figures are positive; otherwise null.
See `finnhub_client._fcf_growth`.

## Missing-data rule (null = fail)

Every filter field must be present and in range; a null fails that screener
(matches TradingView, where a null fails a range filter). Because all fields come
from one Finnhub call, a null reflects genuinely-missing data — there is no flaky
second source to degrade coverage. `LENIENT_FIELDS` in `screeners.py` is an empty
set kept as a toggle if you ever want to restore lenient/`partial_data` behavior.

## Run it

```bash
cd screener
pip install -r requirements.txt
export FINNHUB_API_KEY=your_key_here

python pipeline.py          # one full run to stdout, writes data/results.json + publishes to Supabase
# or run the API + dev scheduler:
python server.py            # serves on :8787
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/screener` | cached results: 4 lists + `last_updated` + run `stats` |
| POST | `/api/screener/refresh` | trigger a re-run in the background (202) |
| GET | `/api/screener/health` | liveness + whether a run is in progress |

## Scheduling

- **Production:** GitHub Actions cron `0 21 * * *` (nightly, 21:00 UTC). GitHub
  cron is UTC-only, so it lands at 23:00 Israel in winter (UTC+2) and 00:00 in
  summer (UTC+3) — a ±1h DST drift we accept.
- **Dev only:** `server.py`'s APScheduler uses the named tz `23:00 Asia/Jerusalem`
  (`config.SCHEDULE_*`); it is not the production path.

## Files

- `config.py` — infra config, units convention, exchange matcher
- `universe.py` — build + dedupe the three source lists
- `finnhub_client.py` — throttled free-tier calls + metric normalization (incl. EV/EBITDA + FCF growth)
- `screeners.py` — filter definitions, PEG, missing-data rule, scoring
- `pipeline.py` — orchestration + phase logging + JSON output + Supabase publish
- `server.py` — FastAPI endpoints + dev APScheduler job

## Known caveats / to verify

- **Growth-metric units**: Finnhub growth/ROE fields are percentages, divided by
  100 to fractions. Verified 2026-06-17 against live AAPL data (see
  `_pct_to_fraction` in `finnhub_client.py`).
- **FCF growth basis** is TTM-vs-last-fiscal-year (see above), not FY-over-FY.
- **Volume pre-filter** is applied in the metric phase, not the pre-filter phase,
  because Finnhub's quote/profile2 expose no volume field.
