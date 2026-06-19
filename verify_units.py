"""One-off: confirm whether Finnhub returns growth/ROE as percent or fraction.

Run:  FINNHUB_API_KEY=your_key python verify_units.py

Decision rule (Apple's real EPS/revenue growth are low single-to-double-digit %):
  prints ~8.2 / ~12.4   -> PERCENT   -> keep the /100 in _pct_to_fraction
  prints ~0.082 / 0.124 -> FRACTION  -> REMOVE the /100 (dividing twice shrinks
                                        every threshold to 1/100th of intended)
"""

import os
import requests

KEY = os.environ.get("FINNHUB_API_KEY")
if not KEY:
    raise SystemExit("Set FINNHUB_API_KEY first:  FINNHUB_API_KEY=... python verify_units.py")

resp = requests.get(
    "https://finnhub.io/api/v1/stock/metric",
    params={"symbol": "AAPL", "metric": "all", "token": KEY},
    timeout=20,
)
resp.raise_for_status()
data = resp.json()["metric"]

for field in ("epsGrowthTTMYoy", "revenueGrowthTTMYoy", "roeTTM"):
    print(f"{field}: {data.get(field)}")

probe = data.get("epsGrowthTTMYoy")
if probe is not None:
    verdict = "PERCENT -> KEEP /100" if abs(probe) > 1.5 else "FRACTION -> REMOVE /100"
    print(f"\nVerdict: looks like {verdict}")
